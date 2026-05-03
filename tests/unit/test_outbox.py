"""Outbox semantics: insert, claim-row atomicity, settle, dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.core.events import make_event
from daeyeon_bot.core.results import Ack, DeadLetter, Retry
from daeyeon_bot.infra import outbox, storage


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


async def _open(db_path: Path) -> aiosqlite.Connection:
    conn = await storage.open_db(db_path)
    await storage.apply_migrations(conn)
    return conn


async def test_insert_event_dedup_by_source_key(db_path: Path, now: datetime) -> None:
    conn = await _open(db_path)
    try:
        e1 = make_event(type="manual.message", payload={"m": "x"}, created_at=now)
        e2 = make_event(type="manual.message", payload={"m": "y"}, created_at=now)
        assert await outbox.insert_event(conn, e1, source="manual", source_dedup_key="k1")
        assert not await outbox.insert_event(conn, e2, source="manual", source_dedup_key="k1")
    finally:
        await conn.close()


async def test_claim_one_is_atomic(db_path: Path, now: datetime) -> None:
    conn = await _open(db_path)
    try:
        ev = make_event(type="manual.message", payload={"m": "hi"}, created_at=now)
        await outbox.insert_event(conn, ev, source="manual", source_dedup_key="k1")
        await outbox.enqueue_handler(conn, event_id=ev.id, handler="echo", now=now)
        await conn.commit()

        first = await outbox.claim_one(conn, claimed_by="proc-A", now=now)
        second = await outbox.claim_one(conn, claimed_by="proc-B", now=now)
        assert first is not None
        assert second is None  # only one outbox row
        assert first.handler == "echo"
        assert first.event.id == ev.id
    finally:
        await conn.close()


async def test_settle_ack_writes_dedup_and_run(db_path: Path, now: datetime) -> None:
    conn = await _open(db_path)
    try:
        ev = make_event(type="manual.message", payload={}, created_at=now)
        await outbox.insert_event(conn, ev, source="manual", source_dedup_key="k1")
        await outbox.enqueue_handler(conn, event_id=ev.id, handler="echo", now=now)
        await conn.commit()
        job = await outbox.claim_one(conn, claimed_by="proc-A", now=now)
        assert job is not None

        await outbox.settle(
            conn,
            job=job,
            result=Ack(),
            started_at=now,
            finished_at=now + timedelta(milliseconds=50),
            dedup_ttl=timedelta(days=1),
        )

        async with conn.execute(
            "SELECT status, attempt FROM outbox WHERE id = ?", (job.outbox_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "acked"
        assert row["attempt"] == 1

        async with conn.execute(
            "SELECT status, duration_ms FROM runs WHERE outbox_id = ?", (job.outbox_id,)
        ) as cur:
            run_row = await cur.fetchone()
        assert run_row is not None
        assert run_row["status"] == "acked"
        assert run_row["duration_ms"] == 50

        deduped = await outbox.is_deduped(
            conn, event_id=ev.id, handler="echo", attempt_epoch=0, now=now
        )
        assert deduped is True
    finally:
        await conn.close()


async def test_retry_schedules_next_attempt(db_path: Path, now: datetime) -> None:
    conn = await _open(db_path)
    try:
        ev = make_event(type="manual.message", payload={}, created_at=now)
        await outbox.insert_event(conn, ev, source="manual", source_dedup_key="k1")
        await outbox.enqueue_handler(conn, event_id=ev.id, handler="echo", now=now)
        await conn.commit()
        job = await outbox.claim_one(conn, claimed_by="proc-A", now=now)
        assert job is not None

        await outbox.settle(
            conn,
            job=job,
            result=Retry(after_s=30.0),
            started_at=now,
            finished_at=now,
            dedup_ttl=None,
        )

        # Cannot reclaim before next_attempt_at.
        none = await outbox.claim_one(conn, claimed_by="proc-A", now=now + timedelta(seconds=10))
        assert none is None

        # Can reclaim after next_attempt_at.
        again = await outbox.claim_one(conn, claimed_by="proc-A", now=now + timedelta(seconds=31))
        assert again is not None
    finally:
        await conn.close()


async def test_dead_letter_persists_reason(db_path: Path, now: datetime) -> None:
    conn = await _open(db_path)
    try:
        ev = make_event(type="manual.message", payload={}, created_at=now)
        await outbox.insert_event(conn, ev, source="manual", source_dedup_key="k1")
        await outbox.enqueue_handler(conn, event_id=ev.id, handler="echo", now=now)
        await conn.commit()
        job = await outbox.claim_one(conn, claimed_by="proc-A", now=now)
        assert job is not None

        await outbox.settle(
            conn,
            job=job,
            result=DeadLetter(reason="boom"),
            started_at=now,
            finished_at=now,
            dedup_ttl=None,
        )

        async with conn.execute(
            "SELECT status, last_error FROM outbox WHERE id = ?", (job.outbox_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "dead_letter"
        assert row["last_error"] == "boom"
    finally:
        await conn.close()
