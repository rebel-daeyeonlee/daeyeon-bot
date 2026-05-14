"""CRUD for `jira_triage_audit` — T033 tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.infra.jira_triage_audit import (
    find_latest,
    insert_audit,
    list_for_issue,
    list_recent,
    record_supersede,
)
from daeyeon_bot.infra.storage import apply_migrations, open_db


async def _seed_event(conn: aiosqlite.Connection, event_id: str, dedup: str) -> None:
    await conn.execute(
        "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
        " payload_json, trace_id, created_at)"
        " VALUES (?,?,1,?,?, '{}', 'tr', '2026-05-13T00:00:00Z')",
        (event_id, "jira.assigned", "jira_assigned", dedup),
    )
    await conn.commit()


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


@pytest.mark.asyncio
async def test_insert_posted_row_round_trips(tmp_path: Path) -> None:
    """A posted row inserted via insert_audit comes back via find_latest."""
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, "e1", "d1")
        posted = datetime(2026, 5, 13, 7, 15, 2, tzinfo=UTC)
        rid = await insert_audit(
            conn,
            event_id="e1",
            issue_key="SSWCI-100",
            comment_seq="1",
            status="posted",
            created_at=datetime(2026, 5, 13, 7, 0, 0, tzinfo=UTC),
            parent_epic_key="SSWCI-99",
            hostname="ssw-giga-02",
            tc_name="TC-0033-x",
            branch="release/v3.2",
            head_sha="abc" * 13 + "x",  # 40 chars
            run_id="2574-1",
            domain="CpFw",
            severity="sev2",
            comment_id="10001",
            posted_at=posted,
            summary_chars=512,
            evidence_count=3,
            persona_skill="daeyeon-bot-jira-triage",
            persona_mtime_ns=1234567890,
        )
        assert rid > 0
        row = await find_latest(conn, "SSWCI-100")
        assert row is not None
        assert row.status == "posted"
        assert row.comment_id == "10001"
        assert row.domain == "CpFw"
        assert row.evidence_count == 3
        assert row.persona_mtime_ns == 1234567890
        assert row.superseded_comment_ids == ()
        assert row.missing_fields == ()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_insert_skipped_row_with_missing_fields(tmp_path: Path) -> None:
    """missing_fields JSON array round-trips."""
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, "e1", "d1")
        await insert_audit(
            conn,
            event_id="e1",
            issue_key="SSWCI-200",
            comment_seq="1",
            status="skipped_missing_metadata",
            created_at=datetime.now(tz=UTC),
            missing_fields=("branch", "commit"),
        )
        row = await find_latest(conn, "SSWCI-200")
        assert row is not None
        assert row.status == "skipped_missing_metadata"
        assert row.missing_fields == ("branch", "commit")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_check_constraint_rejects_unknown_status(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, "e1", "d1")
        with pytest.raises(aiosqlite.IntegrityError):
            await insert_audit(
                conn,
                event_id="e1",
                issue_key="SSWCI-1",
                comment_seq="1",
                status="BOGUS_STATUS",  # type: ignore[arg-type]
                created_at=datetime.now(tz=UTC),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_record_supersede_appends_old_comment_id(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, "e1", "d1")
        rid = await insert_audit(
            conn,
            event_id="e1",
            issue_key="SSWCI-300",
            comment_seq="1",
            status="posted",
            created_at=datetime.now(tz=UTC),
            comment_id="orig-1",
            posted_at=datetime(2026, 5, 13, 14, 30, 11, tzinfo=UTC),
        )
        await record_supersede(
            conn,
            rid,
            new_comment_id="new-2",
            new_posted_at=datetime(2026, 5, 13, 16, 0, 0, tzinfo=UTC),
        )
        await conn.commit()
        row = await find_latest(conn, "SSWCI-300")
        assert row is not None
        assert row.comment_id == "new-2"
        assert row.superseded_comment_ids == ("orig-1",)
        # Second supersede chains.
        await record_supersede(
            conn,
            rid,
            new_comment_id="new-3",
            new_posted_at=datetime(2026, 5, 13, 17, 0, 0, tzinfo=UTC),
        )
        await conn.commit()
        row = await find_latest(conn, "SSWCI-300")
        assert row is not None
        assert row.comment_id == "new-3"
        assert row.superseded_comment_ids == ("orig-1", "new-2")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_list_for_issue_returns_rows_newest_first(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        for i in range(3):
            await _seed_event(conn, f"e{i}", f"d{i}")
            await insert_audit(
                conn,
                event_id=f"e{i}",
                issue_key="SSWCI-99",
                comment_seq="1",
                status="failed",
                created_at=datetime(2026, 5, 13, 7, i, 0, tzinfo=UTC),
            )
        rows = await list_for_issue(conn, issue_key="SSWCI-99")
        assert len(rows) == 3
        # Newest first means highest id first.
        assert rows[0].id > rows[1].id > rows[2].id
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_list_recent_across_issues(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        for i in range(5):
            await _seed_event(conn, f"e{i}", f"d{i}")
            await insert_audit(
                conn,
                event_id=f"e{i}",
                issue_key=f"SSWCI-{i}",
                comment_seq="1",
                status="posted",
                created_at=datetime.now(tz=UTC),
            )
        rows = await list_recent(conn, limit=3)
        assert len(rows) == 3
    finally:
        await conn.close()
