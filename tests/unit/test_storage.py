"""Storage adapter: PRAGMA contract, migration runner."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.infra import storage


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


async def test_open_db_applies_pragmas(db_path: Path) -> None:
    conn = await storage.open_db(db_path)
    try:
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row is not None
        # WAL is the only mode we accept.
        assert row[0].lower() == "wal"

        async with conn.execute("PRAGMA foreign_keys") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert int(row[0]) == 1
    finally:
        await conn.close()


async def test_apply_migrations_creates_schema(db_path: Path) -> None:
    async with storage.connection(db_path) as conn:
        version = await storage.apply_migrations(conn)
        assert version == 1

        # Re-apply should be a no-op.
        version_again = await storage.apply_migrations(conn)
        assert version_again == 1

        # All Phase 1 tables exist.
        for table in ("events", "outbox", "runs", "dedup_keys", "ratelimit_buckets", "quarantine"):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None, f"missing table: {table}"


async def test_outbox_status_check_constraint(db_path: Path) -> None:
    async with storage.connection(db_path) as conn:
        await storage.apply_migrations(conn)
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("e1", "manual.message", 1, "manual", "k1", "{}", "t1", "2026-05-03T00:00:00Z"),
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO outbox(event_id, handler, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("e1", "echo", "bogus", "2026-05-03T00:00:00Z", "2026-05-03T00:00:00Z"),
            )
            await conn.commit()
