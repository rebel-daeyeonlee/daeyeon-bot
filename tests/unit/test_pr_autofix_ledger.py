"""Feature 004 — the comment ledger IS the loop's termination condition."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from daeyeon_bot.infra import pr_autofix_ledger as ledger
from daeyeon_bot.infra.storage import apply_migrations, open_db

REPO = "owner/repo"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def _record(conn: aiosqlite.Connection, cid: int, **kw: object) -> None:
    args: dict[str, object] = {
        "repo": REPO,
        "pr_number": 1,
        "comment_id": cid,
        "comment_kind": "review_comment",
        "author": "coderabbitai[bot]",
        "verdict": "accepted",
        "created_at": NOW,
    }
    args.update(kw)
    await ledger.record_decision(conn, **args)  # type: ignore[arg-type]


async def test_handled_ids_is_what_drains_the_pending_set(tmp_path: Path) -> None:
    conn = await _db(tmp_path)
    try:
        assert await ledger.handled_comment_ids(conn, repo=REPO, pr_number=1) == set()
        await _record(conn, 11)
        await _record(conn, 12, verdict="rejected")
        assert await ledger.handled_comment_ids(conn, repo=REPO, pr_number=1) == {11, 12}
    finally:
        await conn.close()


async def test_rejected_and_deferred_also_count_as_handled(tmp_path: Path) -> None:
    """Otherwise a comment the bot deliberately declined would come back every
    single poll and be re-triaged forever."""
    conn = await _db(tmp_path)
    try:
        await _record(conn, 21, verdict="rejected")
        await _record(conn, 22, verdict="deferred")
        await _record(conn, 23, verdict="failed")
        assert await ledger.handled_comment_ids(conn, repo=REPO, pr_number=1) == {21, 22, 23}
    finally:
        await conn.close()


async def test_ledger_is_scoped_per_pr(tmp_path: Path) -> None:
    conn = await _db(tmp_path)
    try:
        await _record(conn, 31)
        assert await ledger.handled_comment_ids(conn, repo=REPO, pr_number=2) == set()
        assert await ledger.handled_comment_ids(conn, repo="other/repo", pr_number=1) == set()
    finally:
        await conn.close()


async def test_re_recording_upgrades_the_row_instead_of_failing(tmp_path: Path) -> None:
    """A Retry re-entering after a partial reply pass must be able to flip
    `replied` 0 → 1 rather than tripping the UNIQUE constraint."""
    conn = await _db(tmp_path)
    try:
        await _record(conn, 41, replied=False, reason="first pass")
        await _record(conn, 41, replied=True, reason="second pass", commit_sha="a" * 40)
        rows = await ledger.list_for_pr(conn, repo=REPO, pr_number=1)
        assert len(rows) == 1
        assert rows[0].replied
        assert rows[0].reason == "second pass"
        assert rows[0].commit_sha == "a" * 40
    finally:
        await conn.close()


async def test_ledger_survives_deletion_of_its_event(tmp_path: Path) -> None:
    """Retention prunes `events` after 90 days. If the ledger cascaded, those
    comments would re-enter the pending set and get answered a second time."""
    conn = await _db(tmp_path)
    try:
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at) VALUES"
            " ('evt-1', 'gh.pr_feedback', 1, 'gh_pr_feedback', 'k1', '{}', 't1', '2026-01-01')"
        )
        await _record(conn, 51, event_id="evt-1")
        await conn.execute("DELETE FROM events WHERE id = 'evt-1'")
        assert await ledger.handled_comment_ids(conn, repo=REPO, pr_number=1) == {51}
    finally:
        await conn.close()
