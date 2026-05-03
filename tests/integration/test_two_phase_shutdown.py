"""2-phase shutdown drains in-flight handlers up to the budget, then marks
stragglers 'interrupted' so the next boot can decide retry vs DLQ.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daeyeon_bot.app.config import load
from daeyeon_bot.app.container import ContainerOverrides
from daeyeon_bot.app.lifecycle import BootOptions, boot
from daeyeon_bot.core.events import Event, make_event
from daeyeon_bot.core.protocols import HandlerContext
from daeyeon_bot.core.results import Ack, HandlerResult
from daeyeon_bot.handlers import echo as echo_handler
from daeyeon_bot.infra import outbox, storage
from daeyeon_bot.infra.claude import FakeClaudeSession

pytestmark = pytest.mark.integration


def _config(state_dir: Path) -> Path:
    cfg = state_dir.parent / "config.toml"
    cfg.write_text(
        f"""
[runtime]
state_dir = {str(state_dir)!r}

[logging]
level = "WARNING"
format = "console"

[handlers.echo]
enabled = true

[routing]
"manual.message" = ["echo"]
""".lstrip(),
        encoding="utf-8",
    )
    return cfg


async def _enqueue(db_path: Path, event: Event, dedup_key: str, now: datetime) -> None:
    async with storage.connection(db_path) as conn:
        await storage.apply_migrations(conn)
        await outbox.insert_event(conn, event, source="manual", source_dedup_key=dedup_key)
        await outbox.enqueue_handler(conn, event_id=event.id, handler="echo", now=now)
        await conn.commit()


async def test_drain_within_budget_acks_in_flight_work(tmp_path: Path) -> None:
    """A handler that finishes inside the drain budget completes successfully
    even when shutdown is requested while it's running."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg_path = _config(state_dir)
    cfg = load(str(cfg_path))
    db_path = cfg.db_path

    now = datetime.now(tz=UTC)
    event = make_event(type="manual.message", payload={"message": "hi"}, created_at=now)
    await _enqueue(db_path, event, dedup_key=f"e2e-{uuid.uuid4()}", now=now)

    fake = FakeClaudeSession(default="ok")
    stop = asyncio.Event()

    boot_task = asyncio.create_task(
        boot(
            BootOptions(
                config_path=str(cfg_path),
                overrides=ContainerOverrides(claude_session_factory=lambda: fake),
                install_signal_handlers=False,
                external_stop_event=stop,
            )
        )
    )

    # Wait for the handler to actually run before requesting shutdown.
    for _ in range(100):
        if fake.calls:
            break
        await asyncio.sleep(0.05)
    assert fake.calls, "handler never ran"

    stop.set()
    await asyncio.wait_for(boot_task, timeout=5.0)

    async with storage.connection(db_path) as conn:
        async with conn.execute("SELECT status FROM outbox WHERE event_id = ?", (event.id,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "acked"


async def test_drain_timeout_marks_in_flight_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler that exceeds the drain budget gets cancelled and its row
    flipped to 'interrupted'."""
    monkeypatch.setattr("daeyeon_bot.app.lifecycle.PHASE_B_BUDGET_S", 0.5)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg_path = _config(state_dir)
    cfg = load(str(cfg_path))
    db_path = cfg.db_path

    now = datetime.now(tz=UTC)
    event = make_event(type="manual.message", payload={"message": "hi"}, created_at=now)

    handler_in_flight = asyncio.Event()

    class SlowHandler:
        manifest = echo_handler.MANIFEST

        async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
            handler_in_flight.set()
            await asyncio.sleep(60)
            return Ack()

    from daeyeon_bot.app import registry as registry_mod
    from daeyeon_bot.app.registry import HandlerRecord

    original = registry_mod.instantiate_handler

    def patched_instantiate(name: str, entry: object) -> HandlerRecord:
        record = original(name, entry)  # type: ignore[arg-type]
        if name == "echo":
            return replace(record, instance=SlowHandler())
        return record

    monkeypatch.setattr(registry_mod, "instantiate_handler", patched_instantiate)

    await _enqueue(db_path, event, dedup_key=f"e2e-{uuid.uuid4()}", now=now)

    fake = FakeClaudeSession(default="ok")
    stop = asyncio.Event()

    boot_task = asyncio.create_task(
        boot(
            BootOptions(
                config_path=str(cfg_path),
                overrides=ContainerOverrides(claude_session_factory=lambda: fake),
                install_signal_handlers=False,
                external_stop_event=stop,
            )
        )
    )

    await asyncio.wait_for(handler_in_flight.wait(), timeout=5.0)

    stop.set()
    await asyncio.wait_for(boot_task, timeout=10.0)

    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT status, last_error FROM outbox WHERE event_id = ?", (event.id,)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "interrupted"
    assert row["last_error"] is not None
    assert "drain timeout" in row["last_error"]
