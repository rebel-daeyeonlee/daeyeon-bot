"""Retention pruning.

PLAN.md §4.2 retention defaults:
    events_days = 90                # delete events older than this …
                                    #   … whose outbox rows are all settled.
    runs_days = 30                  # delete runs older than this …
    runs_keep_per_handler = 10      #   … unless they're in the most-recent N per handler.
    dedup_default_ttl_days = 7      # dedup_keys cleanup honours each row's expires_at.
    gh_state_dormant_days = 90      # delete dormant gh_review_requested_state AND
                                    #   gh_pr_feedback_state rows (feature 004 shares
                                    #   the knob — both are per-PR polling state).
    dead_letter_days = 30           # delete outbox `dead_letter` rows ahead of events_days.

Events pruning cascades the outbox rows that reference them — but only if
none of those rows are still active (`pending` / `running` / `retry` /
`interrupted`). An event that's still mid-flight stays put.

Dead-letter pruning runs first and is independent of `events_days`: a
`dead_letter` row carries no in-flight semantics (it's already routed
to DLQ for operator replay), so it can age out faster than the general
events retention window. When all of an event's outbox rows are gone,
the subsequent `_prune_events` pass tidies the (now-orphaned) event.

`prune()` is idempotent and returns counts so the CLI can report what shrank.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

from daeyeon_bot.app.config import Config
from daeyeon_bot.infra import pr_feedback_state, pr_review_state


@dataclass(frozen=True, slots=True)
class PruneReport:
    runs_deleted: int
    dedup_keys_deleted: int
    events_deleted: int
    outbox_deleted: int
    gh_state_deleted: int = 0
    dead_letter_deleted: int = 0


async def prune(conn: aiosqlite.Connection, *, config: Config, now: datetime) -> PruneReport:
    """Apply retention. Caller owns the connection lifecycle."""
    runs_deleted = await _prune_runs(
        conn,
        cutoff=now - timedelta(days=config.retention.runs_days),
        keep_per_handler=config.retention.runs_keep_per_handler,
    )
    dedup_deleted = await _prune_dedup_keys(conn, now=now)
    dead_letter_deleted = await _prune_dead_letter_outbox(
        conn,
        cutoff=now - timedelta(days=config.retention.dead_letter_days),
    )
    outbox_deleted, events_deleted = await _prune_events(
        conn,
        cutoff=now - timedelta(days=config.retention.events_days),
    )
    gh_state_cutoff = (now - timedelta(days=config.retention.gh_state_dormant_days)).isoformat()
    gh_state_deleted = await pr_review_state.prune_dormant(conn, older_than_iso=gh_state_cutoff)
    # Feature 004. Note this prunes only the POLLING state; `pr_autofix_comment`
    # is deliberately left alone, so a PR whose state row aged out cannot come
    # back and have its old comments answered a second time.
    gh_state_deleted += await pr_feedback_state.prune_dormant(conn, older_than_iso=gh_state_cutoff)
    await conn.commit()
    return PruneReport(
        runs_deleted=runs_deleted,
        dedup_keys_deleted=dedup_deleted,
        events_deleted=events_deleted,
        outbox_deleted=outbox_deleted + dead_letter_deleted,
        gh_state_deleted=gh_state_deleted,
        dead_letter_deleted=dead_letter_deleted,
    )


async def _prune_runs(
    conn: aiosqlite.Connection, *, cutoff: datetime, keep_per_handler: int
) -> int:
    """Delete runs older than cutoff EXCEPT the latest N per handler.

    Implementation: a window function picks the row-rank inside each handler.
    Anything past keep_per_handler AND older than cutoff goes.
    """
    iso_cutoff = cutoff.isoformat()
    cursor = await conn.execute(
        """
        DELETE FROM runs
         WHERE id IN (
           SELECT id FROM (
             SELECT id, started_at,
                    ROW_NUMBER() OVER (PARTITION BY handler ORDER BY id DESC) AS rk
               FROM runs
           )
           WHERE rk > ? AND started_at < ?
         )
        """,
        (keep_per_handler, iso_cutoff),
    )
    return cursor.rowcount


async def _prune_dedup_keys(conn: aiosqlite.Connection, *, now: datetime) -> int:
    cursor = await conn.execute("DELETE FROM dedup_keys WHERE expires_at < ?", (now.isoformat(),))
    return cursor.rowcount


async def _prune_dead_letter_outbox(conn: aiosqlite.Connection, *, cutoff: datetime) -> int:
    """Delete `status='dead_letter'` outbox rows older than cutoff.

    Independent of the events_days window — dead-letter rows have no
    in-flight semantics, so they can age out aggressively. The follow-up
    `_prune_events` pass cleans up any events that lose their last
    outbox row as a result.
    """
    iso_cutoff = cutoff.isoformat()
    cursor = await conn.execute(
        "DELETE FROM outbox WHERE status = 'dead_letter' AND created_at < ?",
        (iso_cutoff,),
    )
    return cursor.rowcount


_DELETE_OUTBOX_FOR_OLD_SETTLED_EVENTS = """
    DELETE FROM outbox
     WHERE event_id IN (
       SELECT id FROM events
        WHERE created_at < ?
          AND id NOT IN (
            SELECT event_id FROM outbox
             WHERE status IN ('pending','running','retry','interrupted')
          )
     )
"""

_DELETE_OLD_EVENTS_WITHOUT_OUTBOX = """
    DELETE FROM events
     WHERE created_at < ?
       AND id NOT IN (SELECT event_id FROM outbox)
"""


async def _prune_events(conn: aiosqlite.Connection, *, cutoff: datetime) -> tuple[int, int]:
    """Drop old events whose outbox rows are all settled. Returns (outbox, events)."""
    iso_cutoff = cutoff.isoformat()
    outbox_cursor = await conn.execute(_DELETE_OUTBOX_FOR_OLD_SETTLED_EVENTS, (iso_cutoff,))
    outbox_deleted = outbox_cursor.rowcount
    events_cursor = await conn.execute(_DELETE_OLD_EVENTS_WITHOUT_OUTBOX, (iso_cutoff,))
    events_deleted = events_cursor.rowcount
    return outbox_deleted, events_deleted


__all__ = ["PruneReport", "prune"]
