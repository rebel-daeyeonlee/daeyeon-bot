"""Migration 012 — `pr_review_audit.status` gains 'skipped_not_authored'.

`[handlers.pr_review].scope = "self"` (the new default) needs a distinct audit
reason for "this PR isn't mine", the mirror of the pre-existing
'skipped_self_authored'. The CHECK constraint means the handler cannot even
record that skip until this migration lands.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.infra.storage import apply_migrations, migration_files, open_db

_LATEST_SCHEMA_VERSION = 12


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def _seed_event(conn: aiosqlite.Connection, event_id: str) -> None:
    await conn.execute(
        "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
        " payload_json, trace_id, created_at) VALUES"
        f" ('{event_id}','gh.review_requested',1,'gh_review_requested','k-{event_id}',"
        "'{}','t','2026-01-01')"
    )


async def test_skipped_not_authored_is_accepted(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, "e1")
        await conn.execute(
            "INSERT INTO pr_review_audit(event_id, repo, pr_number, head_sha, request_gen,"
            " status, created_at)"
            " VALUES ('e1','o/r',7,'deadbeef','1','skipped_not_authored','2026-01-01')"
        )
        # The statuses earlier migrations added must survive this rebuild.
        await conn.execute(
            "INSERT INTO pr_review_audit(event_id, repo, pr_number, head_sha, request_gen,"
            " status, created_at)"
            " VALUES ('e1','o/r',8,'deadbeef','1','skipped_disallowed_repo','2026-01-01')"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO pr_review_audit(event_id, repo, pr_number, head_sha, request_gen,"
                " status, created_at)"
                " VALUES ('e1','o/r',9,'deadbeef','1','skipped_someone_elses','2026-01-01')"
            )
    finally:
        await conn.close()


async def test_preserves_existing_audit_rows(tmp_path: Path) -> None:
    """The rebuild copies data verbatim — `posted` rows are what the handler
    reads to decide 'already reviewed', so losing one means a double review."""
    conn = await open_db(tmp_path / "state.db")
    try:
        for seq, _name, sql in migration_files():
            if seq > 11:
                break
            await conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
            await conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(seq),),
            )
        await _seed_event(conn, "e2")
        await conn.execute(
            "INSERT INTO pr_review_audit(event_id, repo, pr_number, head_sha, request_gen,"
            " status, review_id, created_at)"
            " VALUES ('e2','o/r',7,'deadbeef','1','posted',4242,'2026-01-01')"
        )
        await conn.commit()

        assert await apply_migrations(conn) == _LATEST_SCHEMA_VERSION
        async with conn.execute(
            "SELECT repo, pr_number, status, review_id FROM pr_review_audit"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        assert rows == [{"repo": "o/r", "pr_number": 7, "status": "posted", "review_id": 4242}]
    finally:
        await conn.close()


async def test_is_idempotent(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        assert await apply_migrations(conn) == _LATEST_SCHEMA_VERSION
    finally:
        await conn.close()
