"""Migration 009 — pr_autofix tables (feature 004)."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.infra.storage import apply_migrations, open_db

_LATEST_SCHEMA_VERSION = 11


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ) as cur:
        return await cur.fetchone() is not None


async def test_creates_the_three_autofix_tables(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        for table in ("gh_pr_feedback_state", "pr_autofix_comment", "pr_autofix_audit"):
            assert await _table_exists(conn, table), table
    finally:
        await conn.close()


async def test_is_idempotent(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        assert await apply_migrations(conn) == _LATEST_SCHEMA_VERSION
    finally:
        await conn.close()


async def test_ledger_has_no_foreign_key_to_events(tmp_path: Path) -> None:
    """Load-bearing. Retention deletes events past `events_days`; a CASCADE here
    would drop ledger rows and let the bot re-answer months-old comments."""
    conn = await _open(tmp_path)
    try:
        async with conn.execute("PRAGMA foreign_key_list(pr_autofix_comment)") as cur:
            assert await cur.fetchall() == []
    finally:
        await conn.close()


async def test_audit_cascades_from_events(tmp_path: Path) -> None:
    """The audit row IS per-event forensics, so it should die with its event."""
    conn = await _open(tmp_path)
    try:
        async with conn.execute("PRAGMA foreign_key_list(pr_autofix_audit)") as cur:
            rows = await cur.fetchall()
        assert [(r["table"], r["on_delete"]) for r in rows] == [("events", "CASCADE")]
    finally:
        await conn.close()


async def test_ledger_rejects_an_unknown_verdict(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO pr_autofix_comment"
                "(repo, pr_number, comment_id, comment_kind, author, verdict, created_at)"
                " VALUES ('o/r', 1, 5, 'review_comment', 'bot', 'maybe', '2026-01-01')"
            )
    finally:
        await conn.close()


async def test_ledger_is_unique_per_comment(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        stmt = (
            "INSERT INTO pr_autofix_comment"
            "(repo, pr_number, comment_id, comment_kind, author, verdict, created_at)"
            " VALUES ('o/r', 1, 5, 'review_comment', 'bot', 'accepted', '2026-01-01')"
        )
        await conn.execute(stmt)
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(stmt)
    finally:
        await conn.close()


async def test_migration_010_allows_the_dry_run_status(tmp_path: Path) -> None:
    """SQLite cannot alter a CHECK, so 010 rebuilds the table. Verify the new
    value is accepted and an unknown one is still refused."""
    conn = await _open(tmp_path)
    try:
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at) VALUES"
            " ('e1', 'gh.pr_feedback', 1, 'gh_pr_feedback', 'k', '{}', 't', '2026-01-01')"
        )
        await conn.execute(
            "INSERT INTO pr_autofix_audit(event_id, repo, pr_number, head_sha, round,"
            " status, created_at) VALUES ('e1','o/r',1,'abc',1,'dry_run','2026-01-01')"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO pr_autofix_audit(event_id, repo, pr_number, head_sha, round,"
                " status, created_at) VALUES ('e1','o/r',2,'abc',1,'shipped?','2026-01-01')"
            )
    finally:
        await conn.close()


async def test_migration_010_preserves_existing_audit_rows(tmp_path: Path) -> None:
    """The rebuild copies data verbatim — a row written before the upgrade must
    survive it, since these rows are the handler's crash guard."""
    conn = await open_db(tmp_path / "state.db")
    try:
        # Stop at 9, insert, then let 010 rebuild underneath the row.
        from daeyeon_bot.infra.storage import migration_files

        for seq, _name, sql in migration_files():
            if seq > 9:
                break
            await conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
            await conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(seq),),
            )
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at) VALUES"
            " ('e9', 'gh.pr_feedback', 1, 'gh_pr_feedback', 'k9', '{}', 't', '2026-01-01')"
        )
        await conn.execute(
            "INSERT INTO pr_autofix_audit(event_id, repo, pr_number, head_sha, round,"
            " status, commit_sha, created_at)"
            " VALUES ('e9','o/r',7,'deadbeef',2,'pushed','abc123','2026-01-01')"
        )
        await conn.commit()

        assert await apply_migrations(conn) == _LATEST_SCHEMA_VERSION
        async with conn.execute(
            "SELECT repo, pr_number, status, commit_sha FROM pr_autofix_audit"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        assert rows == [{"repo": "o/r", "pr_number": 7, "status": "pushed", "commit_sha": "abc123"}]
    finally:
        await conn.close()


async def test_migration_011_allows_comment_only(tmp_path: Path) -> None:
    """`fix_enabled = false` needs its own status: it is neither `dry_run` (a fix
    was made and withheld) nor `all_rejected` (nothing was worth fixing)."""
    conn = await _open(tmp_path)
    try:
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at) VALUES"
            " ('e2','gh.pr_feedback',1,'gh_pr_feedback','k2','{}','t','2026-01-01')"
        )
        await conn.execute(
            "INSERT INTO pr_autofix_audit(event_id, repo, pr_number, head_sha, round,"
            " status, created_at) VALUES ('e2','o/r',1,'abc',1,'comment_only','2026-01-01')"
        )
        # The values 010 added must survive 011's rebuild.
        await conn.execute(
            "INSERT INTO pr_autofix_audit(event_id, repo, pr_number, head_sha, round,"
            " status, created_at) VALUES ('e2','o/r',2,'abc',1,'dry_run','2026-01-01')"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO pr_autofix_audit(event_id, repo, pr_number, head_sha, round,"
                " status, created_at) VALUES ('e2','o/r',3,'abc',1,'commented?','2026-01-01')"
            )
    finally:
        await conn.close()
