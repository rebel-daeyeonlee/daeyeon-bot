"""Per-comment decision ledger for `pr_autofix` (`pr_autofix_comment`).

This table IS the loop's termination condition. The trigger computes

    pending = {live comment ids from reviewers} - {ids already in the ledger}

and emits nothing when that set is empty. So "폴링하다가 추가 지적사항이 없으면
멈춘다" is not a special case in the poll loop — it falls out of an empty set
difference.

Two consequences worth stating, because they are easy to break later:

  * A row is written only once the comment has been REPLIED TO on GitHub. If a
    run dies between triage and reply, the comment stays pending and the next
    poll picks it up again. Writing rows at triage time would be faster but
    would silently swallow feedback on any crash.
  * The table has no foreign key to `events`. Retention prunes events after
    `events_days`; a cascade here would resurrect months-old comments into the
    pending set and make the bot answer them a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from daeyeon_bot.core.pr_autofix.types import CommentKind, Verdict

# `failed` is ledger-only (never a `TriageDecision.verdict`): the comment was
# answered with "봇이 처리하지 못했다" so it must not stay pending forever.
LedgerVerdict = Verdict | str


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One row of `pr_autofix_comment`."""

    id: int
    repo: str
    pr_number: int
    comment_id: int
    comment_kind: str
    author: str
    event_id: str | None
    verdict: str
    reason: str | None
    commit_sha: str | None
    replied: bool
    created_at: str


async def handled_comment_ids(conn: aiosqlite.Connection, *, repo: str, pr_number: int) -> set[int]:
    """IDs already decided for this PR. The subtrahend of the pending set."""
    async with conn.execute(
        "SELECT comment_id FROM pr_autofix_comment WHERE repo = ? AND pr_number = ?",
        (repo, pr_number),
    ) as cur:
        rows = await cur.fetchall()
    return {int(row["comment_id"]) for row in rows}


async def list_for_pr(conn: aiosqlite.Connection, *, repo: str, pr_number: int) -> list[LedgerRow]:
    """Full ledger for a PR, oldest first. Used by `inspect` and tests."""
    async with conn.execute(
        "SELECT id, repo, pr_number, comment_id, comment_kind, author, event_id,"
        " verdict, reason, commit_sha, replied, created_at"
        " FROM pr_autofix_comment WHERE repo = ? AND pr_number = ? ORDER BY id",
        (repo, pr_number),
    ) as cur:
        rows = await cur.fetchall()
    return [
        LedgerRow(
            id=int(row["id"]),
            repo=str(row["repo"]),
            pr_number=int(row["pr_number"]),
            comment_id=int(row["comment_id"]),
            comment_kind=str(row["comment_kind"]),
            author=str(row["author"]),
            event_id=row["event_id"] if row["event_id"] is None else str(row["event_id"]),
            verdict=str(row["verdict"]),
            reason=row["reason"] if row["reason"] is None else str(row["reason"]),
            commit_sha=row["commit_sha"] if row["commit_sha"] is None else str(row["commit_sha"]),
            replied=bool(row["replied"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


async def record_decision(
    conn: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    comment_id: int,
    comment_kind: CommentKind | str,
    author: str,
    verdict: LedgerVerdict,
    created_at: datetime,
    event_id: str | None = None,
    reason: str | None = None,
    commit_sha: str | None = None,
    replied: bool = False,
) -> None:
    """Write (or refresh) the decision for one comment.

    `INSERT ... ON CONFLICT DO UPDATE` rather than a plain INSERT: a Retry that
    re-enters after a partial reply pass must be able to upgrade an existing
    row from `replied = 0` to `replied = 1` instead of tripping the UNIQUE
    constraint. Single statement, so it stays inside the caller's transaction
    without a read-modify-write.
    """
    await conn.execute(
        "INSERT INTO pr_autofix_comment"
        "(repo, pr_number, comment_id, comment_kind, author, event_id, verdict,"
        " reason, commit_sha, replied, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo, pr_number, comment_id) DO UPDATE SET"
        " verdict = excluded.verdict, reason = excluded.reason,"
        " commit_sha = excluded.commit_sha, replied = excluded.replied,"
        " event_id = excluded.event_id",
        (
            repo,
            pr_number,
            comment_id,
            str(comment_kind),
            author,
            event_id,
            str(verdict),
            reason,
            commit_sha,
            1 if replied else 0,
            created_at.isoformat(),
        ),
    )


__all__ = [
    "LedgerRow",
    "LedgerVerdict",
    "handled_comment_ids",
    "list_for_pr",
    "record_decision",
]
