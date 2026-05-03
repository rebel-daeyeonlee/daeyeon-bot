"""Phase 1 vertical slice e2e: config → boot → fire manual → echo Acks.

Boots the daemon under FakeClaudeSession in-process. Fires an event by
calling the same outbox writer the CLI uses. Asserts the outbox row
reaches 'acked' within a budget, and a runs row was written.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daeyeon_bot.app.config import load
from daeyeon_bot.app.container import ContainerOverrides
from daeyeon_bot.app.lifecycle import BootOptions, boot
from daeyeon_bot.core.events import make_event
from daeyeon_bot.infra import outbox, storage
from daeyeon_bot.infra.claude import FakeClaudeSession

pytestmark = pytest.mark.integration


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def config_file(tmp_path: Path, state_dir: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
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
    return cfg_path


async def test_phase1_vertical_slice(
    config_file: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClaudeSession(default="ok-from-fake")
    overrides = ContainerOverrides(claude_session_factory=lambda: fake)
    stop = asyncio.Event()

    boot_task = asyncio.create_task(
        boot(
            BootOptions(
                config_path=str(config_file),
                overrides=overrides,
                install_signal_handlers=False,
                external_stop_event=stop,
            )
        )
    )

    cfg = load(str(config_file))
    db_path = cfg.db_path

    # Wait for the daemon to open the DB and apply migrations.
    for _ in range(50):
        if db_path.exists():
            break
        await asyncio.sleep(0.05)
    assert db_path.exists(), "daemon never created state.db"

    # Fire the event by writing through the same outbox API the CLI uses.
    now = datetime.now(tz=UTC)
    event = make_event(type="manual.message", payload={"message": "hello"}, created_at=now)
    async with storage.connection(db_path) as conn:
        await storage.apply_migrations(conn)
        await outbox.insert_event(
            conn, event, source="manual", source_dedup_key=f"e2e-{uuid.uuid4()}"
        )
        await outbox.enqueue_handler(conn, event_id=event.id, handler="echo", now=now)
        await conn.commit()

    # Poll for the row to settle.
    settled_status: str | None = None
    for _ in range(100):
        async with storage.connection(db_path) as conn:
            async with conn.execute(
                "SELECT status FROM outbox WHERE event_id = ?", (event.id,)
            ) as cur:
                row = await cur.fetchone()
        if row is not None and row["status"] == "acked":
            settled_status = "acked"
            break
        await asyncio.sleep(0.05)

    # Stop the daemon via the injected stop event. boot returns cleanly.
    stop.set()
    await asyncio.wait_for(boot_task, timeout=5.0)

    assert settled_status == "acked"
    assert fake.calls and fake.calls[0]["prompt"] == "hello"

    # Audit row exists.
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT status, duration_ms FROM runs WHERE event_id = ?", (event.id,)
        ) as cur:
            run_row = await cur.fetchone()
    assert run_row is not None
    assert run_row["status"] == "acked"
    assert isinstance(run_row["duration_ms"], int)
