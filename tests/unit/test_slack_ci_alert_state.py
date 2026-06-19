"""CRUD for `slack_ci_alert_state` per-channel cursor (feature 003)."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from daeyeon_bot.infra.slack_ci_alert_state import (
    advance_cursor,
    get_cursor,
    list_cursors,
    seed_cursor,
    touch,
)
from daeyeon_bot.infra.storage import apply_migrations, open_db


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def test_cold_start_absent_then_seeded(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        assert await get_cursor(conn, "C1") is None
        await seed_cursor(
            conn, channel_id="C1", last_seen_ts="100.5", now_iso="2026-06-19T00:00:00Z"
        )
        await conn.commit()
        row = await get_cursor(conn, "C1")
        assert row is not None
        assert row.seeded is True
        assert row.last_seen_ts == "100.5"
    finally:
        await conn.close()


async def test_seed_is_idempotent(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await seed_cursor(
            conn, channel_id="C1", last_seen_ts="100.5", now_iso="2026-06-19T00:00:00Z"
        )
        await seed_cursor(
            conn, channel_id="C1", last_seen_ts="200.5", now_iso="2026-06-19T01:00:00Z"
        )
        await conn.commit()
        row = await get_cursor(conn, "C1")
        assert row is not None
        assert row.last_seen_ts == "200.5"
    finally:
        await conn.close()


async def test_advance_and_touch(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await seed_cursor(
            conn, channel_id="C1", last_seen_ts="100.5", now_iso="2026-06-19T00:00:00Z"
        )
        await advance_cursor(
            conn, channel_id="C1", last_seen_ts="300.7", now_iso="2026-06-19T02:00:00Z"
        )
        await conn.commit()
        row = await get_cursor(conn, "C1")
        assert row is not None
        assert row.last_seen_ts == "300.7"
        assert row.updated_at == "2026-06-19T02:00:00Z"

        await touch(conn, channel_id="C1", now_iso="2026-06-19T03:00:00Z")
        await conn.commit()
        row2 = await get_cursor(conn, "C1")
        assert row2 is not None
        assert row2.last_seen_ts == "300.7"  # unchanged
        assert row2.updated_at == "2026-06-19T03:00:00Z"
    finally:
        await conn.close()


async def test_list_cursors(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await seed_cursor(conn, channel_id="C2", last_seen_ts="2.0", now_iso="2026-06-19T00:00:00Z")
        await seed_cursor(conn, channel_id="C1", last_seen_ts="1.0", now_iso="2026-06-19T00:00:00Z")
        await conn.commit()
        rows = await list_cursors(conn)
        assert [r.channel_id for r in rows] == ["C1", "C2"]
    finally:
        await conn.close()
