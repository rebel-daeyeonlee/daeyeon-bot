"""Dispatcher: claim → handle → settle for the happy path and key failure modes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.app.config import Config, HandlerEntry
from daeyeon_bot.app.dispatcher import MAX_TRANSIENT_ATTEMPTS, Dispatcher
from daeyeon_bot.app.registry import build_handler_registry
from daeyeon_bot.core.errors import TransientError
from daeyeon_bot.core.events import Event, make_event
from daeyeon_bot.core.protocols import HandlerContext
from daeyeon_bot.core.results import HandlerResult
from daeyeon_bot.infra import outbox, storage
from daeyeon_bot.infra.claude import FakeClaudeSession


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


async def _seed(conn: aiosqlite.Connection, *, dedup_key: str, message: str, now: datetime) -> str:
    ev = make_event(type="manual.message", payload={"message": message}, created_at=now)
    await outbox.insert_event(conn, ev, source="manual", source_dedup_key=dedup_key)
    await outbox.enqueue_handler(conn, event_id=ev.id, handler="echo", now=now)
    await conn.commit()
    return ev.id


async def _build_dispatcher(db: aiosqlite.Connection, fake: FakeClaudeSession) -> Dispatcher:
    cfg = Config(
        handlers={"echo": HandlerEntry()},
        routing={"manual.message": ["echo"]},
    )
    handlers = build_handler_registry(cfg)
    return Dispatcher(
        db=db,
        handlers=handlers,
        claude_session_factory=lambda: fake,
        poll_interval_s=0.05,
    )


async def test_happy_path_acks(db_path: Path, now: datetime) -> None:
    conn = await storage.open_db(db_path)
    await storage.apply_migrations(conn)
    try:
        ev_id = await _seed(conn, dedup_key="k1", message="hi", now=now)
        fake = FakeClaudeSession(default="ok")
        dispatcher = await _build_dispatcher(conn, fake)

        async def stop_when_acked() -> None:
            for _ in range(50):
                async with conn.execute(
                    "SELECT status FROM outbox WHERE event_id = ?", (ev_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row is not None and row["status"] == "acked":
                    dispatcher.stop()
                    return
                await asyncio.sleep(0.05)
            dispatcher.stop()
            raise AssertionError("outbox row never reached 'acked'")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(dispatcher.run())
            tg.create_task(stop_when_acked())

        async with conn.execute(
            "SELECT status, attempt FROM outbox WHERE event_id = ?", (ev_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "acked"
        assert row["attempt"] == 1
        assert fake.calls and fake.calls[0]["prompt"] == "hi"

        async with conn.execute("SELECT status FROM runs WHERE event_id = ?", (ev_id,)) as cur:
            run_row = await cur.fetchone()
        assert run_row is not None
        assert run_row["status"] == "acked"
    finally:
        await conn.close()


async def test_transient_error_becomes_retry(db_path: Path, now: datetime) -> None:
    conn = await storage.open_db(db_path)
    await storage.apply_migrations(conn)
    try:
        ev_id = await _seed(conn, dedup_key="k2", message="hi", now=now)
        fake = FakeClaudeSession(default="ok")
        dispatcher = await _build_dispatcher(conn, fake)

        record = dispatcher.handlers.by_name["echo"]
        original_instance = record.instance
        call_count = {"n": 0}

        class FlakyHandler:
            manifest = record.manifest

            async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise TransientError("network blip")
                return await original_instance.handle(event, ctx)  # type: ignore[attr-defined]

        from dataclasses import replace as _replace

        dispatcher.handlers.by_name["echo"] = _replace(record, instance=FlakyHandler())

        async def stop_when(status_target: str) -> None:
            for _ in range(50):
                async with conn.execute(
                    "SELECT status FROM outbox WHERE event_id = ?", (ev_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row is not None and row["status"] == status_target:
                    dispatcher.stop()
                    return
                await asyncio.sleep(0.05)
            dispatcher.stop()
            raise AssertionError(f"outbox row never reached {status_target!r}")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(dispatcher.run())
            tg.create_task(stop_when("retry"))

        async with conn.execute(
            "SELECT status, next_attempt_at FROM outbox WHERE event_id = ?", (ev_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "retry"
        assert row["next_attempt_at"] is not None
    finally:
        await conn.close()


async def test_transient_error_dead_letters_at_max_attempts(db_path: Path, now: datetime) -> None:
    """Once attempt count reaches MAX_TRANSIENT_ATTEMPTS, dispatcher dead-letters.

    Without this cap, a flaky upstream that raises TransientError every call
    pins a row in retry forever (we observed attempt=142 in production).
    """
    conn = await storage.open_db(db_path)
    await storage.apply_migrations(conn)
    try:
        ev_id = await _seed(conn, dedup_key="k_max", message="hi", now=now)
        # Pre-bump the row so the next claim is the boundary attempt.
        await conn.execute(
            "UPDATE outbox SET attempt = ? WHERE event_id = ?",
            (MAX_TRANSIENT_ATTEMPTS - 1, ev_id),
        )
        await conn.commit()

        fake = FakeClaudeSession(default="ok")
        dispatcher = await _build_dispatcher(conn, fake)
        record = dispatcher.handlers.by_name["echo"]

        class AlwaysTransientHandler:
            manifest = record.manifest

            async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
                raise TransientError("upstream blip")

        from dataclasses import replace as _replace

        dispatcher.handlers.by_name["echo"] = _replace(record, instance=AlwaysTransientHandler())

        async def stop_when(status_target: str) -> None:
            for _ in range(50):
                async with conn.execute(
                    "SELECT status FROM outbox WHERE event_id = ?", (ev_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row is not None and row["status"] == status_target:
                    dispatcher.stop()
                    return
                await asyncio.sleep(0.05)
            dispatcher.stop()
            raise AssertionError(f"outbox row never reached {status_target!r}")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(dispatcher.run())
            tg.create_task(stop_when("dead_letter"))

        async with conn.execute(
            "SELECT status, attempt, last_error FROM outbox WHERE event_id = ?", (ev_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "dead_letter"
        assert row["attempt"] == MAX_TRANSIENT_ATTEMPTS
        assert row["last_error"] is not None
        assert "max transient attempts" in row["last_error"]
    finally:
        await conn.close()


async def test_handler_concurrency_two_runs_in_parallel(db_path: Path, now: datetime) -> None:
    """With `concurrency=2`, two events for the same handler run simultaneously.

    Guards the `[handlers.pr_review].concurrency` config knob (E6): the
    dispatcher's per-handler semaphore is keyed off `manifest.concurrency`,
    so a config override flows through `_override_manifest`. The test uses
    `echo` for simplicity — the wiring is identical for `pr_review`.
    """
    conn = await storage.open_db(db_path)
    await storage.apply_migrations(conn)
    try:
        ev1 = await _seed(conn, dedup_key="par_1", message="hi-1", now=now)
        ev2 = await _seed(conn, dedup_key="par_2", message="hi-2", now=now)

        cfg = Config(
            handlers={"echo": HandlerEntry(concurrency=2)},
            routing={"manual.message": ["echo"]},
        )
        handlers = build_handler_registry(cfg)
        fake = FakeClaudeSession(default="ok")
        dispatcher = Dispatcher(
            db=conn,
            handlers=handlers,
            claude_session_factory=lambda: fake,
            poll_interval_s=0.05,
        )

        record = handlers.by_name["echo"]
        both_started = asyncio.Event()
        in_flight = {"n": 0}
        gate = asyncio.Event()

        class BarrierHandler:
            manifest = record.manifest

            async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
                in_flight["n"] += 1
                if in_flight["n"] >= 2:
                    both_started.set()
                await gate.wait()
                return await record.instance.handle(event, ctx)  # type: ignore[attr-defined]

        from dataclasses import replace as _replace

        dispatcher.handlers.by_name["echo"] = _replace(record, instance=BarrierHandler())

        async def watcher() -> None:
            try:
                await asyncio.wait_for(both_started.wait(), timeout=2.0)
            finally:
                gate.set()
            for _ in range(50):
                async with conn.execute(
                    "SELECT COUNT(*) AS n FROM outbox WHERE event_id IN (?, ?) AND status = 'acked'",
                    (ev1, ev2),
                ) as cur:
                    row = await cur.fetchone()
                if row is not None and row["n"] == 2:
                    dispatcher.stop()
                    return
                await asyncio.sleep(0.05)
            dispatcher.stop()
            raise AssertionError("both rows never reached 'acked'")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(dispatcher.run())
            tg.create_task(watcher())

        assert both_started.is_set()
        assert record.manifest.concurrency == 2
    finally:
        await conn.close()


async def test_permanent_error_becomes_dead_letter(db_path: Path, now: datetime) -> None:
    conn = await storage.open_db(db_path)
    await storage.apply_migrations(conn)
    try:
        # Empty message → ValidationError → DeadLetter
        ev_id = await _seed(conn, dedup_key="k3", message="", now=now)
        # The above call seeded message="" but EchoHandler needs non-empty —
        # it will raise ValidationError, which is PermanentError.
        fake = FakeClaudeSession(default="ok")
        dispatcher = await _build_dispatcher(conn, fake)

        async def stop_when(status_target: str) -> None:
            for _ in range(50):
                async with conn.execute(
                    "SELECT status FROM outbox WHERE event_id = ?", (ev_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row is not None and row["status"] == status_target:
                    dispatcher.stop()
                    return
                await asyncio.sleep(0.05)
            dispatcher.stop()
            raise AssertionError(f"outbox row never reached {status_target!r}")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(dispatcher.run())
            tg.create_task(stop_when("dead_letter"))

        async with conn.execute(
            "SELECT status, last_error FROM outbox WHERE event_id = ?", (ev_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "dead_letter"
        assert row["last_error"] is not None
        assert "ValidationError" in row["last_error"]
    finally:
        await conn.close()
