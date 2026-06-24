"""Migration 002 — gh_review_requested_state + pr_review_audit smoke test."""

from __future__ import annotations

from pathlib import Path

import pytest

from daeyeon_bot.infra.storage import apply_migrations, open_db

_LATEST_SCHEMA_VERSION = 8


@pytest.mark.asyncio
async def test_migration_002_brings_schema_to_version_2(tmp_path: Path) -> None:
    """After apply_migrations, schema_version is current and both 002 tables exist.

    Named for migration 002 historically; now asserts the running tip
    (currently 5 — adds jira_assigned_state + jira_triage_audit, see 005).
    """
    db_path = tmp_path / "state.db"
    conn = await open_db(db_path)
    try:
        version = await apply_migrations(conn)
        assert version == _LATEST_SCHEMA_VERSION

        async with conn.execute("SELECT value FROM meta WHERE key = 'schema_version'") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["value"] == str(_LATEST_SCHEMA_VERSION)

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("gh_review_requested_state",),
        ) as cur:
            assert await cur.fetchone() is not None

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("pr_review_audit",),
        ) as cur:
            assert await cur.fetchone() is not None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_002_is_idempotent(tmp_path: Path) -> None:
    """Re-running apply_migrations on an already-migrated DB is a no-op."""
    db_path = tmp_path / "state.db"
    conn = await open_db(db_path)
    try:
        await apply_migrations(conn)
        version_again = await apply_migrations(conn)
        assert version_again == _LATEST_SCHEMA_VERSION
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pr_review_audit_status_check_constraint(tmp_path: Path) -> None:
    """pr_review_audit.status enum is enforced by CHECK."""
    import aiosqlite

    db_path = tmp_path / "state.db"
    conn = await open_db(db_path)
    try:
        await apply_migrations(conn)
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES ('e1','t',1,'src','k','{}','tr','2026-01-01T00:00:00Z')"
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO pr_review_audit(event_id, repo, pr_number, head_sha,"
                " request_gen, status, created_at)"
                " VALUES ('e1','o/r',1,'abc','1','BOGUS','2026-01-01T00:00:00Z')"
            )
            await conn.commit()
    finally:
        await conn.close()
