"""Migration 007 — ci_triage_audit recurrence columns (feature 003 P2)."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from daeyeon_bot.infra.storage import apply_migrations, open_db


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def test_schema_version_is_7(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        async with conn.execute("SELECT value FROM meta WHERE key = 'schema_version'") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert int(row["value"]) == 7
    finally:
        await conn.close()


async def test_recurrence_columns_exist(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        async with conn.execute("PRAGMA table_info(ci_triage_audit)") as cur:
            cols = {str(r["name"]) for r in await cur.fetchall()}
        assert {"dut_host", "signature"} <= cols
    finally:
        await conn.close()


async def test_is_idempotent(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        again = await apply_migrations(conn)
        assert again == 7
    finally:
        await conn.close()
