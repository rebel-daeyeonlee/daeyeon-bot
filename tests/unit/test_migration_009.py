"""Migration 009 — pr_autofix tables (feature 004)."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.infra.storage import apply_migrations, open_db

_LATEST_SCHEMA_VERSION = 9


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
