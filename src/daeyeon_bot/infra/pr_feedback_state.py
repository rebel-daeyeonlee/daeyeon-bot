"""CRUD + state machine for `gh_pr_feedback_state` (feature 004).

Shaped after `infra/pr_review_state.py`, with one difference that matters:
`pr_review`'s generation counter bumps on every new head SHA, because a new
SHA means "review this again". Here a new head SHA is often OUR OWN push, so
the SHA is recorded but never on its own a reason to act. What decides whether
the handler runs is the pending-comment set the trigger computes — see
`triggers/gh_pr_feedback.py`.

`round` counts how many autofix events we have emitted for a PR. It is the
runaway brake: bot fixes → bot re-reviews → new comments → bot fixes … is a
loop with no natural bound, and `[handlers.pr_autofix].max_rounds` puts one on
it. The counter resets when the PR leaves and re-enters the observed set.

The caller owns the transaction so the state UPSERT and the `events` INSERT
commit together.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True, slots=True)
class FeedbackStateRow:
    """One row of `gh_pr_feedback_state`."""

    repo: str
    pr_number: int
    head_sha: str
    round: int
    in_pending_set: bool
    last_observed_at: str


async def get_state(
    conn: aiosqlite.Connection, repo: str, pr_number: int
) -> FeedbackStateRow | None:
    """Return the persisted row for `(repo, pr_number)`, or None."""
    async with conn.execute(
        "SELECT repo, pr_number, head_sha, round, in_pending_set, last_observed_at"
        " FROM gh_pr_feedback_state WHERE repo = ? AND pr_number = ?",
        (repo, pr_number),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return _to_row(row)


async def select_all(conn: aiosqlite.Connection) -> dict[tuple[str, int], FeedbackStateRow]:
    """Snapshot every state row, keyed by `(repo, pr_number)`."""
    async with conn.execute(
        "SELECT repo, pr_number, head_sha, round, in_pending_set, last_observed_at"
        " FROM gh_pr_feedback_state"
    ) as cur:
        rows = await cur.fetchall()
    out: dict[tuple[str, int], FeedbackStateRow] = {}
    for raw in rows:
        row = _to_row(raw)
        out[(row.repo, row.pr_number)] = row
    return out


async def mark_observed(
    conn: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    now_iso: str,
) -> FeedbackStateRow:
    """Record that the PR is present in this poll, without consuming a round.

    Called for EVERY observed PR, including ones with nothing pending — the
    row's existence is what lets `mark_withdrawn` recognize a PR that closed.
    A PR returning after a withdrawal resets `round` to 0: the operator merged
    or reopened, and the previous run's ping-pong budget should not follow it.
    """
    existing = await get_state(conn, repo, pr_number)
    if existing is None:
        await conn.execute(
            "INSERT INTO gh_pr_feedback_state"
            "(repo, pr_number, head_sha, round, in_pending_set, last_observed_at)"
            " VALUES (?, ?, ?, 0, 1, ?)",
            (repo, pr_number, head_sha, now_iso),
        )
        return FeedbackStateRow(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            round=0,
            in_pending_set=True,
            last_observed_at=now_iso,
        )
    new_round = 0 if not existing.in_pending_set else existing.round
    await conn.execute(
        "UPDATE gh_pr_feedback_state"
        " SET head_sha = ?, round = ?, in_pending_set = 1, last_observed_at = ?"
        " WHERE repo = ? AND pr_number = ?",
        (head_sha, new_round, now_iso, repo, pr_number),
    )
    return FeedbackStateRow(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        round=new_round,
        in_pending_set=True,
        last_observed_at=now_iso,
    )


async def consume_round(
    conn: aiosqlite.Connection, *, repo: str, pr_number: int, now_iso: str
) -> int:
    """Increment and return the round counter. Called only when an event is emitted."""
    await conn.execute(
        "UPDATE gh_pr_feedback_state"
        " SET round = round + 1, last_observed_at = ?"
        " WHERE repo = ? AND pr_number = ?",
        (now_iso, repo, pr_number),
    )
    row = await get_state(conn, repo, pr_number)
    return row.round if row is not None else 1


async def mark_withdrawn(
    conn: aiosqlite.Connection, *, repo: str, pr_number: int, now_iso: str
) -> None:
    """Flip a no-longer-observed PR to dormant (merged, closed, or filtered out)."""
    await conn.execute(
        "UPDATE gh_pr_feedback_state"
        " SET in_pending_set = 0, last_observed_at = ?"
        " WHERE repo = ? AND pr_number = ? AND in_pending_set = 1",
        (now_iso, repo, pr_number),
    )


async def prune_dormant(conn: aiosqlite.Connection, *, older_than_iso: str) -> int:
    """Delete dormant rows last observed before `older_than_iso`.

    Pending rows are never pruned — they track live PRs. Note this deletes only
    the polling state; the `pr_autofix_comment` ledger is intentionally kept
    longer, so a pruned-then-reopened PR does not get its old comments
    re-answered.
    """
    cur = await conn.execute(
        "DELETE FROM gh_pr_feedback_state WHERE in_pending_set = 0 AND last_observed_at < ?",
        (older_than_iso,),
    )
    deleted = cur.rowcount or 0
    await cur.close()
    return int(deleted)


def _to_row(row: aiosqlite.Row) -> FeedbackStateRow:
    return FeedbackStateRow(
        repo=str(row["repo"]),
        pr_number=int(row["pr_number"]),
        head_sha=str(row["head_sha"]),
        round=int(row["round"]),
        in_pending_set=bool(row["in_pending_set"]),
        last_observed_at=str(row["last_observed_at"]),
    )


__all__ = [
    "FeedbackStateRow",
    "consume_round",
    "get_state",
    "mark_observed",
    "mark_withdrawn",
    "prune_dormant",
    "select_all",
]
