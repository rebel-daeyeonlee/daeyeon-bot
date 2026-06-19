"""CRUD for `ci_triage_audit` (feature 003 P1)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from daeyeon_bot.infra.ci_triage_audit import (
    find_posted_for_message,
    find_posted_for_run,
    insert_audit,
    list_recent,
)
from daeyeon_bot.infra.storage import apply_migrations, open_db

_NOW = datetime(2026, 6, 19, 7, 15, 2, tzinfo=UTC)


async def _seed_event(conn: aiosqlite.Connection, event_id: str, dedup: str) -> None:
    await conn.execute(
        "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
        " payload_json, trace_id, created_at)"
        " VALUES (?, 'slack.ci_alert', 1, 'slack_ci_alert', ?, '{}', 'tr',"
        " '2026-06-19T00:00:00Z')",
        (event_id, dedup),
    )
    await conn.commit()


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def test_posted_row_round_trips(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, "e1", "d1")
        rid = await insert_audit(
            conn,
            event_id="e1",
            channel_id="C1",
            message_ts="100.1",
            status="posted",
            created_at=_NOW,
            repo="rebellions-sw/ssw-bundle",
            run_id="27758520154",
            pr_number=3890,
            failed_jobs=("premerge / result",),
            attribution="infra_env",
            classification="environment",
            owner_area="DevOps",
            confidence="medium",
            wiki_matches=("wiki/oncall/incidents/qemu-golden-base-image-missing.md",),
            posted_channel_id="C_DRYRUN",
            posted_message_ts="200.2",
            summary_chars=412,
        )
        await conn.commit()
        assert rid > 0

        found = await find_posted_for_message(conn, channel_id="C1", message_ts="100.1")
        assert found is not None
        assert found.attribution == "infra_env"
        assert found.owner_area == "DevOps"
        assert found.failed_jobs == ("premerge / result",)
        assert found.wiki_matches == ("wiki/oncall/incidents/qemu-golden-base-image-missing.md",)
    finally:
        await conn.close()


async def test_secondary_run_guard_matches_only_posted(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, "e1", "d1")
        await insert_audit(
            conn,
            event_id="e1",
            channel_id="C1",
            message_ts="100.1",
            status="posted",
            created_at=_NOW,
            repo="rebellions-sw/ssw-bundle",
            run_id="999",
        )
        await conn.commit()
        hit = await find_posted_for_run(conn, repo="rebellions-sw/ssw-bundle", run_id="999")
        assert hit is not None
        miss = await find_posted_for_run(conn, repo="rebellions-sw/ssw-bundle", run_id="111")
        assert miss is None
    finally:
        await conn.close()


async def test_null_run_rows_never_collapse(tmp_path: Path) -> None:
    """Two link-less alerts (repo/run NULL) must NOT match each other — SQL
    NULL = NULL is never true, which is the intended behavior."""
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, "e1", "d1")
        await _seed_event(conn, "e2", "d2")
        await insert_audit(
            conn,
            event_id="e1",
            channel_id="C1",
            message_ts="100.1",
            status="skipped_no_run_link",
            created_at=_NOW,
        )
        await insert_audit(
            conn,
            event_id="e2",
            channel_id="C1",
            message_ts="100.2",
            status="skipped_no_run_link",
            created_at=_NOW,
        )
        await conn.commit()
        # A NULL-keyed lookup must find nothing (and the rows do not collapse).
        miss = await find_posted_for_run(conn, repo=None, run_id=None)  # type: ignore[arg-type]
        assert miss is None
    finally:
        await conn.close()


async def test_list_recent_newest_first(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        for i in range(3):
            await _seed_event(conn, f"e{i}", f"d{i}")
            await insert_audit(
                conn,
                event_id=f"e{i}",
                channel_id="C1",
                message_ts=f"100.{i}",
                status="posted",
                created_at=_NOW,
            )
        await conn.commit()
        rows = await list_recent(conn, limit=10)
        assert [r.message_ts for r in rows] == ["100.2", "100.1", "100.0"]
    finally:
        await conn.close()
