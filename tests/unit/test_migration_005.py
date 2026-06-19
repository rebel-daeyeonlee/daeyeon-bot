"""Migration 005 — jira_assigned_state + jira_triage_audit smoke test."""

from __future__ import annotations

from pathlib import Path

import pytest

from daeyeon_bot.infra.storage import apply_migrations, open_db

_LATEST_SCHEMA_VERSION = 6


@pytest.mark.asyncio
async def test_migration_005_brings_schema_to_version_5(tmp_path: Path) -> None:
    """After apply_migrations, schema_version is 5 and both 005 tables exist."""
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
            ("jira_assigned_state",),
        ) as cur:
            assert await cur.fetchone() is not None

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("jira_triage_audit",),
        ) as cur:
            assert await cur.fetchone() is not None

        # FR-004a — cold-start seed flag exists, default '0'.
        async with conn.execute(
            "SELECT value FROM meta WHERE key = 'jira_assigned_state_seeded'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["value"] == "0"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_005_is_idempotent(tmp_path: Path) -> None:
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
async def test_jira_triage_audit_status_check_constraint(tmp_path: Path) -> None:
    """jira_triage_audit.status enum is enforced by CHECK."""
    import aiosqlite

    db_path = tmp_path / "state.db"
    conn = await open_db(db_path)
    try:
        await apply_migrations(conn)
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES ('e5','t',1,'src','k5','{}','tr','2026-05-13T00:00:00Z')"
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO jira_triage_audit(event_id, issue_key, comment_seq,"
                " status, created_at)"
                " VALUES ('e5','SSWCI-1','1','BOGUS','2026-05-13T00:00:00Z')"
            )
            await conn.commit()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_jira_triage_audit_accepts_all_status_enum_values(tmp_path: Path) -> None:
    """Every value in the CHECK enum must INSERT cleanly."""
    db_path = tmp_path / "state.db"
    conn = await open_db(db_path)
    try:
        await apply_migrations(conn)
        # Pre-seed 7 events so each audit row has a distinct event_id FK.
        statuses = (
            "posted",
            "skipped_not_regression_failure",
            "skipped_missing_metadata",
            "skipped_unresolvable_commit",
            "skipped_submodule_failure",
            "skipped_already_triaged",
            "failed",
        )
        for idx, _ in enumerate(statuses):
            await conn.execute(
                "INSERT INTO events(id, type, schema_version, source,"
                " source_dedup_key, payload_json, trace_id, created_at)"
                " VALUES (?,?,1,?,?, '{}', 'tr', '2026-05-13T00:00:00Z')",
                (f"e{idx}", "jira.assigned", "jira_assigned", f"dedup-{idx}"),
            )
        await conn.commit()

        for idx, status in enumerate(statuses):
            await conn.execute(
                "INSERT INTO jira_triage_audit(event_id, issue_key, comment_seq,"
                " status, created_at)"
                " VALUES (?,?,?,?, '2026-05-13T00:00:00Z')",
                (f"e{idx}", f"SSWCI-{idx}", "1", status),
            )
        await conn.commit()

        async with conn.execute("SELECT COUNT(*) AS c FROM jira_triage_audit") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["c"] == len(statuses)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_jira_assigned_state_primary_key_dedup(tmp_path: Path) -> None:
    """issue_key is PK; second INSERT with same key raises IntegrityError."""
    import aiosqlite

    db_path = tmp_path / "state.db"
    conn = await open_db(db_path)
    try:
        await apply_migrations(conn)
        await conn.execute(
            "INSERT INTO jira_assigned_state(issue_key, project, in_pending_set,"
            " assignment_gen, last_observed_at)"
            " VALUES ('SSWCI-1','SSWCI',1,1,'2026-05-13T00:00:00Z')"
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO jira_assigned_state(issue_key, project, in_pending_set,"
                " assignment_gen, last_observed_at)"
                " VALUES ('SSWCI-1','SSWCI',1,1,'2026-05-13T00:00:00Z')"
            )
            await conn.commit()
    finally:
        await conn.close()
