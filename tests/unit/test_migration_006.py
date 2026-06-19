"""Migration 006 — slack_ci_alert_state + ci_triage_audit (feature 003)."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from daeyeon_bot.infra.storage import apply_migrations, open_db


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def test_schema_version_is_6(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        async with conn.execute("SELECT value FROM meta WHERE key = 'schema_version'") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert int(row["value"]) == 6
    finally:
        await conn.close()


async def test_tables_exist(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('slack_ci_alert_state', 'ci_triage_audit')"
        ) as cur:
            names = {str(r["name"]) for r in await cur.fetchall()}
        assert names == {"slack_ci_alert_state", "ci_triage_audit"}
    finally:
        await conn.close()


async def test_audit_event_fk_cascades_on_event_delete(tmp_path: Path) -> None:
    """Deleting an events row cascades its ci_triage_audit child under
    foreign_keys=ON — the retention-prune regression (plan §Data Model)."""
    conn = await _open(tmp_path)
    try:
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES ('e1', 'slack.ci_alert', 1, 'slack_ci_alert', 'd1', '{}', 'tr',"
            " '2026-06-19T00:00:00Z')",
        )
        await conn.execute(
            "INSERT INTO ci_triage_audit(event_id, channel_id, message_ts, status, created_at)"
            " VALUES ('e1', 'C1', '100.1', 'posted', '2026-06-19T00:00:00Z')",
        )
        await conn.commit()

        await conn.execute("DELETE FROM events WHERE id = 'e1'")
        await conn.commit()

        async with conn.execute("SELECT COUNT(*) AS n FROM ci_triage_audit") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert int(row["n"]) == 0
    finally:
        await conn.close()


async def test_status_check_rejects_unknown_status(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES ('e2', 'slack.ci_alert', 1, 'slack_ci_alert', 'd2', '{}', 'tr',"
            " '2026-06-19T00:00:00Z')",
        )
        await conn.commit()
        try:
            await conn.execute(
                "INSERT INTO ci_triage_audit(event_id, channel_id, message_ts, status, created_at)"
                " VALUES ('e2', 'C1', '100.2', 'not_a_real_status', '2026-06-19T00:00:00Z')",
            )
        except aiosqlite.IntegrityError:
            return
        raise AssertionError("status CHECK should have rejected 'not_a_real_status'")
    finally:
        await conn.close()
