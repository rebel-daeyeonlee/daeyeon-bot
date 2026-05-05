"""CRUD for `pr_review_audit` (data-model.md §1).

Append-mostly: one row per posted (or skipped) review attempt. On
force-supersede the existing row's `superseded_review_ids` JSON array
gets the prior `review_id` pushed; `review_id`/`submitted_at` are then
overwritten with the new values.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, cast

import aiosqlite

from daeyeon_bot.core.pr_review.audit import AuditRow

AuditStatus = Literal[
    "posted",
    "skipped_self_authored",
    "skipped_withdrawn",
    "skipped_too_large",
    "skipped_already_reviewed",
    "skipped_disallowed_repo",
    "failed",
]


async def insert_audit(
    conn: aiosqlite.Connection,
    *,
    event_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    request_gen: str,
    status: AuditStatus,
    created_at: datetime,
    review_id: int | None = None,
    submitted_at: datetime | None = None,
    summary_chars: int | None = None,
    inline_comment_count: int | None = None,
    persona_skill: str | None = None,
    persona_mtime_ns: int | None = None,
    error: str | None = None,
) -> int:
    """Insert one audit row; return the new `id`."""
    cursor = await conn.execute(
        "INSERT INTO pr_review_audit("
        " event_id, repo, pr_number, head_sha, request_gen, status,"
        " review_id, submitted_at, summary_chars, inline_comment_count,"
        " persona_skill, persona_mtime_ns, error, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            repo,
            pr_number,
            head_sha,
            request_gen,
            status,
            review_id,
            submitted_at.isoformat() if submitted_at is not None else None,
            summary_chars,
            inline_comment_count,
            persona_skill,
            persona_mtime_ns,
            error,
            created_at.isoformat(),
        ),
    )
    new_id = cursor.lastrowid
    await cursor.close()
    if new_id is None:
        raise RuntimeError("INSERT INTO pr_review_audit returned no rowid")
    return int(new_id)


async def find_latest(
    conn: aiosqlite.Connection,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> AuditRow | None:
    """Return the most recent audit row for `(repo, pr_number, head_sha)`, or None."""
    async with conn.execute(
        "SELECT id, event_id, repo, pr_number, head_sha, request_gen, status,"
        " review_id, submitted_at, summary_chars, inline_comment_count,"
        " superseded_review_ids, persona_skill, persona_mtime_ns, error, created_at"
        " FROM pr_review_audit"
        " WHERE repo = ? AND pr_number = ? AND head_sha = ?"
        " ORDER BY id DESC LIMIT 1",
        (repo, pr_number, head_sha),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_audit(row)


async def list_for_pr(
    conn: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    limit: int = 50,
) -> list[AuditRow]:
    """Audit rows for one PR, newest first. Used by `inspect pr-review --pr ...`."""
    async with conn.execute(
        "SELECT id, event_id, repo, pr_number, head_sha, request_gen, status,"
        " review_id, submitted_at, summary_chars, inline_comment_count,"
        " superseded_review_ids, persona_skill, persona_mtime_ns, error, created_at"
        " FROM pr_review_audit"
        " WHERE repo = ? AND pr_number = ?"
        " ORDER BY id DESC LIMIT ?",
        (repo, pr_number, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_audit(r) for r in rows]


async def list_recent(
    conn: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[AuditRow]:
    """Most recent audit rows across every PR, newest first."""
    async with conn.execute(
        "SELECT id, event_id, repo, pr_number, head_sha, request_gen, status,"
        " review_id, submitted_at, summary_chars, inline_comment_count,"
        " superseded_review_ids, persona_skill, persona_mtime_ns, error, created_at"
        " FROM pr_review_audit"
        " ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_audit(r) for r in rows]


async def record_supersede(
    conn: aiosqlite.Connection,
    audit_id: int,
    *,
    new_review_id: int,
    new_submitted_at: datetime,
) -> None:
    """Append the existing row's `review_id` to `superseded_review_ids` and set new fields."""
    async with conn.execute(
        "SELECT review_id, superseded_review_ids FROM pr_review_audit WHERE id = ?",
        (audit_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"pr_review_audit row {audit_id} disappeared")
    old_review_id_raw = row["review_id"]
    raw_history = row["superseded_review_ids"] or "[]"
    try:
        history_loaded: object = json.loads(raw_history)
    except (TypeError, ValueError):
        history_loaded = []
    history: list[int] = []
    if isinstance(history_loaded, list):
        history_items = cast("list[Any]", history_loaded)
        history = [int(item) for item in history_items]
    if old_review_id_raw is not None:
        history.append(int(old_review_id_raw))
    await conn.execute(
        "UPDATE pr_review_audit SET review_id = ?, submitted_at = ?,"
        " superseded_review_ids = ?, status = 'posted'"
        " WHERE id = ?",
        (
            new_review_id,
            new_submitted_at.isoformat(),
            json.dumps(history),
            audit_id,
        ),
    )


def _row_to_audit(row: aiosqlite.Row) -> AuditRow:
    raw_history = row["superseded_review_ids"] or "[]"
    try:
        history_loaded: object = json.loads(raw_history)
    except (TypeError, ValueError):
        history_loaded = []
    history: tuple[int, ...] = ()
    if isinstance(history_loaded, list):
        history_items = cast("list[Any]", history_loaded)
        history = tuple(int(item) for item in history_items)
    return AuditRow(
        id=int(row["id"]),
        event_id=str(row["event_id"]),
        repo=str(row["repo"]),
        pr_number=int(row["pr_number"]),
        head_sha=str(row["head_sha"]),
        request_gen=str(row["request_gen"]),
        status=str(row["status"]),
        review_id=int(row["review_id"]) if row["review_id"] is not None else None,
        submitted_at=datetime.fromisoformat(row["submitted_at"])
        if row["submitted_at"] is not None
        else None,
        summary_chars=int(row["summary_chars"]) if row["summary_chars"] is not None else None,
        inline_comment_count=int(row["inline_comment_count"])
        if row["inline_comment_count"] is not None
        else None,
        superseded_review_ids=history,
        persona_skill=str(row["persona_skill"]) if row["persona_skill"] is not None else None,
        persona_mtime_ns=int(row["persona_mtime_ns"])
        if row["persona_mtime_ns"] is not None
        else None,
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


__all__ = [
    "AuditStatus",
    "find_latest",
    "insert_audit",
    "list_for_pr",
    "list_recent",
    "record_supersede",
]
