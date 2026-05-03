"""Boot-time interrupted-row recovery: idempotent → pending, else → DLQ."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.core.events import make_event
from daeyeon_bot.infra import outbox, storage


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncGenerator[aiosqlite.Connection, None]:
    conn = await storage.open_db(tmp_path / "state.db")
    await storage.apply_migrations(conn)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_running_row(
    conn: aiosqlite.Connection, *, handler: str, dedup_key: str, now: datetime
) -> int:
    ev = make_event(type="manual.message", payload={"m": "hi"}, created_at=now)
    await outbox.insert_event(conn, ev, source="manual", source_dedup_key=dedup_key)
    outbox_id = await outbox.enqueue_handler(conn, event_id=ev.id, handler=handler, now=now)
    await conn.execute(
        """
        UPDATE outbox SET status='running', claimed_by='dispatcher-1', claimed_at=?,
               updated_at=? WHERE id=?
        """,
        (now.isoformat(), now.isoformat(), outbox_id),
    )
    await conn.commit()
    return outbox_id


async def test_running_rows_are_marked_interrupted(
    conn: aiosqlite.Connection, now: datetime
) -> None:
    outbox_id = await _seed_running_row(conn, handler="echo", dedup_key="k1", now=now)
    report = await outbox.recover_interrupted_rows(
        conn, idempotent_handlers={"echo"}, known_handlers={"echo"}, now=now
    )
    assert report.crashed == 1
    assert report.rerun == 1
    assert report.dead_lettered == 0

    async with conn.execute(
        "SELECT status, claimed_by, claimed_at FROM outbox WHERE id = ?", (outbox_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None


async def test_idempotent_handler_rerun_path(conn: aiosqlite.Connection, now: datetime) -> None:
    outbox_id = await _seed_running_row(conn, handler="echo", dedup_key="k1", now=now)
    await outbox.recover_interrupted_rows(
        conn, idempotent_handlers={"echo"}, known_handlers={"echo"}, now=now
    )
    async with conn.execute("SELECT status FROM outbox WHERE id = ?", (outbox_id,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "pending"


async def test_non_idempotent_handler_routed_to_dead_letter(
    conn: aiosqlite.Connection, now: datetime
) -> None:
    outbox_id = await _seed_running_row(conn, handler="post-slack", dedup_key="k1", now=now)
    report = await outbox.recover_interrupted_rows(
        conn,
        idempotent_handlers=set(),  # post-slack is not idempotent here
        known_handlers={"post-slack"},
        now=now,
    )
    assert report.dead_lettered == 1
    async with conn.execute(
        "SELECT status, last_error FROM outbox WHERE id = ?", (outbox_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "dead_letter"
    assert "non-idempotent" in (row["last_error"] or "")


async def test_unknown_handler_routed_to_dead_letter(
    conn: aiosqlite.Connection, now: datetime
) -> None:
    outbox_id = await _seed_running_row(conn, handler="ghost", dedup_key="k1", now=now)
    report = await outbox.recover_interrupted_rows(
        conn, idempotent_handlers=set(), known_handlers=set(), now=now
    )
    assert report.dead_lettered == 1
    async with conn.execute(
        "SELECT status, last_error FROM outbox WHERE id = ?", (outbox_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "dead_letter"
    assert "not registered" in (row["last_error"] or "")


async def test_mark_interrupted_only_touches_running_rows(
    conn: aiosqlite.Connection, now: datetime
) -> None:
    running_id = await _seed_running_row(conn, handler="echo", dedup_key="k1", now=now)
    # Seed a 'pending' row that mark_interrupted must NOT touch.
    ev = make_event(type="manual.message", payload={}, created_at=now)
    await outbox.insert_event(conn, ev, source="manual", source_dedup_key="k2")
    pending_id = await outbox.enqueue_handler(conn, event_id=ev.id, handler="echo", now=now)
    await conn.commit()

    affected = await outbox.mark_interrupted(
        conn, outbox_ids=[running_id, pending_id], now=now, reason="drain timeout"
    )
    assert affected == 1

    async with conn.execute("SELECT status FROM outbox WHERE id = ?", (running_id,)) as cur:
        row = await cur.fetchone()
    assert row is not None and row["status"] == "interrupted"

    async with conn.execute("SELECT status FROM outbox WHERE id = ?", (pending_id,)) as cur:
        row = await cur.fetchone()
    assert row is not None and row["status"] == "pending"
