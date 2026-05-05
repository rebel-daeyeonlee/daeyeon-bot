"""Dispatcher gates on the rate-limit bucket — exhausted bucket blocks claims.

Pairs with `tests/unit/test_ratelimit.py`. The unit tests pin `take()`'s
SQL contract; this one proves the dispatcher actually consults the bucket
and that the row stays `pending` (not `interrupted`/`dead_letter`) when
the bucket is empty — which is the whole point of putting the gate
*before* `claim_one()` per OPTIMIZATION_PLAN §A3.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daeyeon_bot.app import ratelimit
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
    """Bucket starts empty (capacity=1, refill=0) and we pre-drain it post-boot.

    Refill stays at 0.0 for the assertion window, then the test bumps it
    so the row finally goes through.
    """
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"""
[runtime]
state_dir = {str(state_dir)!r}

[logging]
level = "WARNING"
format = "console"

[ratelimit]
claude_call_capacity = 1.0
claude_call_refill_per_sec = 0.0

[handlers.echo]
enabled = true

[routing]
"manual.message" = ["echo"]
""".lstrip(),
        encoding="utf-8",
    )
    return cfg_path


async def _wait_for_status(db_path: Path, *, event_id: str, status: str, attempts: int) -> bool:
    for _ in range(attempts):
        async with storage.connection(db_path) as conn:
            async with conn.execute(
                "SELECT status FROM outbox WHERE event_id = ?", (event_id,)
            ) as cur:
                row = await cur.fetchone()
        if row is not None and row["status"] == status:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_dispatcher_blocks_when_bucket_exhausted(config_file: Path, state_dir: Path) -> None:
    fake = FakeClaudeSession(default="ok")
    overrides = ContainerOverrides(claude_session_factory=lambda: fake)
    stop = asyncio.Event()
    cfg = load(str(config_file))

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

    db_path = cfg.db_path
    for _ in range(50):
        if db_path.exists():
            break
        await asyncio.sleep(0.05)
    assert db_path.exists()

    # The dispatcher's first poll cycle consumes the lone token, leaving the
    # bucket exhausted with refill_per_sec=0.0. Wait until that has happened
    # so the enqueue below races against an empty bucket.
    async def _bucket_empty() -> bool:
        async with storage.connection(db_path) as conn:
            async with conn.execute(
                "SELECT tokens FROM ratelimit_buckets WHERE name = ?",
                (ratelimit.CLAUDE_CALL_BUCKET,),
            ) as cur:
                row = await cur.fetchone()
        return row is not None and float(row["tokens"]) < 1.0

    for _ in range(50):
        if await _bucket_empty():
            break
        await asyncio.sleep(0.05)
    assert await _bucket_empty()

    now_utc = datetime.now(tz=UTC)
    event = make_event(type="manual.message", payload={"message": "hi"}, created_at=now_utc)
    async with storage.connection(db_path) as conn:
        await outbox.insert_event(
            conn, event, source="manual", source_dedup_key=f"rl-{uuid.uuid4()}"
        )
        await outbox.enqueue_handler(conn, event_id=event.id, handler="echo", now=now_utc)
        await conn.commit()

    # While the bucket is empty (refill_per_sec=0.0) the dispatcher never claims.
    settled = await _wait_for_status(db_path, event_id=event.id, status="acked", attempts=10)
    assert settled is False

    # Confirm the row is still `pending` and `attempt` was not burned by the
    # rate-limit cycle — that's the design promise from §A3.
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT status, attempt FROM outbox WHERE event_id = ?", (event.id,)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["attempt"] == 0

    # Refuel the bucket and confirm dispatch resumes.
    async with storage.connection(db_path) as conn:
        await ratelimit.upsert_bucket(
            conn,
            name=ratelimit.CLAUDE_CALL_BUCKET,
            capacity=10.0,
            refill_per_sec=10.0,
            now_iso=datetime.now(tz=UTC).isoformat(),
        )
        # Set tokens to capacity so the next take() succeeds without waiting.
        await conn.execute(
            "UPDATE ratelimit_buckets SET tokens = capacity WHERE name = ?",
            (ratelimit.CLAUDE_CALL_BUCKET,),
        )
        await conn.commit()

    assert await _wait_for_status(db_path, event_id=event.id, status="acked", attempts=100)

    stop.set()
    await asyncio.wait_for(boot_task, timeout=5.0)
