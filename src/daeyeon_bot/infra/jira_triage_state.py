"""CRUD + state machine for `jira_assigned_state` (data-model.md §5).

Mirror of `infra/pr_review_state.py` but keyed on `issue_key` instead of
`(repo, pr_number)`. The polling trigger calls `upsert_observation()`
once per `issue_key` in the union of "issues in this poll" union "issues
we have a state row for". The function returns `(assignment_gen,
should_emit)` so the trigger can decide whether to write an `events` row
in the same transaction.

This module performs only the SELECT + INSERT/UPDATE; the caller manages
the surrounding transaction so the events INSERT and the state UPSERT
commit atomically.

Cold-start seed flag (`meta.jira_assigned_state_seeded`) is read/written
by `seed_marker_*` helpers — FR-004a prevents day-1 retroactive triage.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True, slots=True)
class StateRow:
    """One row of `jira_assigned_state`."""

    issue_key: str
    project: str
    in_pending_set: bool
    assignment_gen: int
    last_observed_at: str


async def get_state(
    conn: aiosqlite.Connection,
    issue_key: str,
) -> StateRow | None:
    """Return the persisted state row for `issue_key`, or None."""
    async with conn.execute(
        "SELECT issue_key, project, in_pending_set, assignment_gen, last_observed_at"
        " FROM jira_assigned_state WHERE issue_key = ?",
        (issue_key,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return StateRow(
        issue_key=str(row["issue_key"]),
        project=str(row["project"]),
        in_pending_set=bool(row["in_pending_set"]),
        assignment_gen=int(row["assignment_gen"]),
        last_observed_at=str(row["last_observed_at"]),
    )


async def upsert_observation(
    conn: aiosqlite.Connection,
    *,
    issue_key: str,
    project: str,
    observed_now: bool,
    now_iso: str,
) -> tuple[int, bool]:
    """Apply the §5 case table for one `issue_key` observation.

    Returns `(assignment_gen, should_emit)`:
        assignment_gen — the *current* generation after this observation
        should_emit    — True if the trigger should INSERT an event
    """
    existing = await get_state(conn, issue_key)

    # CASE 1: row IS NULL AND observed_now → INSERT, gen=1, emit.
    if existing is None and observed_now:
        await conn.execute(
            "INSERT INTO jira_assigned_state(issue_key, project, in_pending_set,"
            " assignment_gen, last_observed_at) VALUES (?, ?, 1, 1, ?)",
            (issue_key, project, now_iso),
        )
        return (1, True)

    if existing is None:
        # row IS NULL AND NOT observed_now — caller iterated over a strict
        # superset; nothing to do.
        return (0, False)

    # CASE 2: was withdrawn, now observed → bump gen, flip pending=1, emit.
    if not existing.in_pending_set and observed_now:
        new_gen = existing.assignment_gen + 1
        await conn.execute(
            "UPDATE jira_assigned_state"
            " SET in_pending_set = 1, assignment_gen = ?, last_observed_at = ?"
            " WHERE issue_key = ?",
            (new_gen, now_iso, issue_key),
        )
        return (new_gen, True)

    # CASE 3: pending and still observed → touch only.
    if existing.in_pending_set and observed_now:
        await conn.execute(
            "UPDATE jira_assigned_state SET last_observed_at = ? WHERE issue_key = ?",
            (now_iso, issue_key),
        )
        return (existing.assignment_gen, False)

    # CASE 4: pending but no longer observed → flip pending=0.
    if existing.in_pending_set and not observed_now:
        await conn.execute(
            "UPDATE jira_assigned_state"
            " SET in_pending_set = 0, last_observed_at = ?"
            " WHERE issue_key = ?",
            (now_iso, issue_key),
        )
        return (existing.assignment_gen, False)

    # CASE 5: dormant and still not observed → no change.
    return (existing.assignment_gen, False)


async def seed_cold_start(
    conn: aiosqlite.Connection,
    *,
    observed: list[tuple[str, str]],  # [(issue_key, project)]
    now_iso: str,
) -> int:
    """Seed `jira_assigned_state` with in_pending_set=1 for every observed
    issue WITHOUT emitting events. FR-004a — prevents day-1 thundering-herd.

    Returns the number of rows inserted. Existing rows are left untouched
    (the caller checked `seed_marker_is_set()` first; in the racey case
    where two cold-starts happen we no-op via PK conflict tolerance).
    """
    inserted = 0
    for issue_key, project in observed:
        try:
            await conn.execute(
                "INSERT INTO jira_assigned_state(issue_key, project, in_pending_set,"
                " assignment_gen, last_observed_at) VALUES (?, ?, 1, 1, ?)",
                (issue_key, project, now_iso),
            )
        except aiosqlite.IntegrityError:
            # PK collision — row already exists. Treat as "already seeded".
            continue
        inserted += 1
    return inserted


async def seed_marker_is_set(conn: aiosqlite.Connection) -> bool:
    """Return True if cold-start seed has already run (meta flag = '1')."""
    async with conn.execute(
        "SELECT value FROM meta WHERE key = 'jira_assigned_state_seeded'"
    ) as cur:
        row = await cur.fetchone()
    return row is not None and str(row["value"]) == "1"


async def seed_marker_set(conn: aiosqlite.Connection) -> None:
    """Mark the cold-start seed as complete. Idempotent."""
    await conn.execute(
        "INSERT INTO meta(key, value) VALUES ('jira_assigned_state_seeded', '1')"
        " ON CONFLICT(key) DO UPDATE SET value = '1'"
    )


async def prune_dormant(
    conn: aiosqlite.Connection,
    *,
    older_than_iso: str,
) -> int:
    """Delete dormant state rows last observed before `older_than_iso`.

    A row is *dormant* when `in_pending_set = 0`. Pending rows are never
    pruned — they represent live assignments the operator hasn't dealt
    with yet.
    """
    cur = await conn.execute(
        "DELETE FROM jira_assigned_state WHERE in_pending_set = 0 AND last_observed_at < ?",
        (older_than_iso,),
    )
    deleted = cur.rowcount or 0
    await cur.close()
    return int(deleted)


__all__ = [
    "StateRow",
    "get_state",
    "prune_dormant",
    "seed_cold_start",
    "seed_marker_is_set",
    "seed_marker_set",
    "upsert_observation",
]
