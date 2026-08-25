"""CRUD for `pr_autofix_audit` — one row per `pr_autofix` event (feature 004).

The row doubles as the handler's crash guard. `pr_autofix` is the daemon's only
non-idempotent handler because `git push` cannot be undone, so re-entry after a
crash must be decided from durable state rather than hope:

    status == 'pushed'       → the work is done; Ack without touching GitHub.
    status == 'in_progress'  → we died somewhere between "about to commit" and
                               "pushed". A second attempt could double-push, so
                               the handler DeadLetters for the operator instead.
    anything else / no row   → safe to run.

`open_run()` writes the `in_progress` row; `finish_run()` overwrites it with the
terminal status. Both are single statements.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import aiosqlite

AutofixStatus = Literal[
    "in_progress",
    "pushed",
    "no_changes",
    "all_rejected",
    "verify_failed",
    "skipped_not_author",
    "skipped_disallowed_repo",
    "skipped_max_rounds",
    "skipped_closed",
    "skipped_nothing_pending",
    "skipped_diff_too_large",
    "skipped_protected_path",
    "failed",
]


@dataclass(frozen=True, slots=True)
class AutofixAuditRow:
    """One row of `pr_autofix_audit`."""

    id: int
    event_id: str
    repo: str
    pr_number: int
    head_sha: str
    round: int
    status: str
    accepted_count: int | None
    rejected_count: int | None
    deferred_count: int | None
    commit_sha: str | None
    pushed_at: str | None
    changed_files: int | None
    changed_lines: int | None
    verify_command: str | None
    verify_exit_code: int | None
    persona_skill: str | None
    persona_mtime_ns: int | None
    error: str | None
    created_at: str


async def find_by_event(conn: aiosqlite.Connection, event_id: str) -> AutofixAuditRow | None:
    """Most recent audit row for `event_id`, or None. The re-entry guard."""
    async with conn.execute(
        "SELECT * FROM pr_autofix_audit WHERE event_id = ? ORDER BY id DESC LIMIT 1",
        (event_id,),
    ) as cur:
        row = await cur.fetchone()
    return None if row is None else _to_row(row)


async def insert_audit(
    conn: aiosqlite.Connection,
    *,
    event_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    round: int,  # shadows the builtin deliberately — mirrors the column name.
    status: AutofixStatus,
    created_at: datetime,
    accepted_count: int | None = None,
    rejected_count: int | None = None,
    deferred_count: int | None = None,
    commit_sha: str | None = None,
    pushed_at: datetime | None = None,
    changed_files: int | None = None,
    changed_lines: int | None = None,
    verify_command: str | None = None,
    verify_exit_code: int | None = None,
    persona_skill: str | None = None,
    persona_mtime_ns: int | None = None,
    error: str | None = None,
) -> int:
    """Insert one audit row; return its `id`."""
    cursor = await conn.execute(
        "INSERT INTO pr_autofix_audit("
        " event_id, repo, pr_number, head_sha, round, status,"
        " accepted_count, rejected_count, deferred_count, commit_sha, pushed_at,"
        " changed_files, changed_lines, verify_command, verify_exit_code,"
        " persona_skill, persona_mtime_ns, error, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            repo,
            pr_number,
            head_sha,
            round,
            status,
            accepted_count,
            rejected_count,
            deferred_count,
            commit_sha,
            pushed_at.isoformat() if pushed_at is not None else None,
            changed_files,
            changed_lines,
            verify_command,
            verify_exit_code,
            persona_skill,
            persona_mtime_ns,
            error,
            created_at.isoformat(),
        ),
    )
    new_id = cursor.lastrowid
    await cursor.close()
    if new_id is None:
        raise RuntimeError("INSERT INTO pr_autofix_audit returned no rowid")
    return int(new_id)


async def finish_run(
    conn: aiosqlite.Connection,
    audit_id: int,
    *,
    status: AutofixStatus,
    accepted_count: int | None = None,
    rejected_count: int | None = None,
    deferred_count: int | None = None,
    commit_sha: str | None = None,
    pushed_at: datetime | None = None,
    changed_files: int | None = None,
    changed_lines: int | None = None,
    verify_command: str | None = None,
    verify_exit_code: int | None = None,
    error: str | None = None,
) -> None:
    """Move an `in_progress` row to its terminal status. Single UPDATE."""
    await conn.execute(
        "UPDATE pr_autofix_audit SET"
        " status = ?, accepted_count = ?, rejected_count = ?, deferred_count = ?,"
        " commit_sha = ?, pushed_at = ?, changed_files = ?, changed_lines = ?,"
        " verify_command = ?, verify_exit_code = ?, error = ?"
        " WHERE id = ?",
        (
            status,
            accepted_count,
            rejected_count,
            deferred_count,
            commit_sha,
            pushed_at.isoformat() if pushed_at is not None else None,
            changed_files,
            changed_lines,
            verify_command,
            verify_exit_code,
            error,
            audit_id,
        ),
    )


def _to_row(row: aiosqlite.Row) -> AutofixAuditRow:
    def _opt_int(key: str) -> int | None:
        value = row[key]
        return None if value is None else int(value)

    def _opt_str(key: str) -> str | None:
        value = row[key]
        return None if value is None else str(value)

    return AutofixAuditRow(
        id=int(row["id"]),
        event_id=str(row["event_id"]),
        repo=str(row["repo"]),
        pr_number=int(row["pr_number"]),
        head_sha=str(row["head_sha"]),
        round=int(row["round"]),
        status=str(row["status"]),
        accepted_count=_opt_int("accepted_count"),
        rejected_count=_opt_int("rejected_count"),
        deferred_count=_opt_int("deferred_count"),
        commit_sha=_opt_str("commit_sha"),
        pushed_at=_opt_str("pushed_at"),
        changed_files=_opt_int("changed_files"),
        changed_lines=_opt_int("changed_lines"),
        verify_command=_opt_str("verify_command"),
        verify_exit_code=_opt_int("verify_exit_code"),
        persona_skill=_opt_str("persona_skill"),
        persona_mtime_ns=_opt_int("persona_mtime_ns"),
        error=_opt_str("error"),
        created_at=str(row["created_at"]),
    )


__all__ = ["AutofixAuditRow", "AutofixStatus", "find_by_event", "finish_run", "insert_audit"]
