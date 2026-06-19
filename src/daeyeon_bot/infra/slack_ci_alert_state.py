"""CRUD for `slack_ci_alert_state` (migration 006) — per-channel read cursor.

Unlike `jira_assigned_state` / `gh_review_requested_state` (per-entity membership
flags), a Slack channel is a *stream*, so this is a single high-water cursor row
per channel. The 5-case state machine that drives cursor advance / cold-start
seed / staleness re-seed lives in the `slack_ci_alert` trigger (feature 003 P2);
this module performs only the SELECT + INSERT/UPDATE so the caller owns the
surrounding transaction (events INSERT + cursor advance commit atomically per
emitted candidate).
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True, slots=True)
class CursorRow:
    """One row of `slack_ci_alert_state`."""

    channel_id: str
    last_seen_ts: str
    seeded: bool
    updated_at: str


async def get_cursor(
    conn: aiosqlite.Connection,
    channel_id: str,
) -> CursorRow | None:
    """Return the persisted cursor row for `channel_id`, or None (never polled)."""
    async with conn.execute(
        "SELECT channel_id, last_seen_ts, seeded, updated_at"
        " FROM slack_ci_alert_state WHERE channel_id = ?",
        (channel_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return CursorRow(
        channel_id=str(row["channel_id"]),
        last_seen_ts=str(row["last_seen_ts"]),
        seeded=bool(row["seeded"]),
        updated_at=str(row["updated_at"]),
    )


async def seed_cursor(
    conn: aiosqlite.Connection,
    *,
    channel_id: str,
    last_seen_ts: str,
    now_iso: str,
) -> None:
    """Cold-start: anchor the cursor to the channel's current latest ts and mark
    seeded. Emits nothing (caller emits no event for the seed). Idempotent via
    PK upsert so a racey double cold-start does not crash."""
    await conn.execute(
        "INSERT INTO slack_ci_alert_state(channel_id, last_seen_ts, seeded, updated_at)"
        " VALUES (?, ?, 1, ?)"
        " ON CONFLICT(channel_id) DO UPDATE SET"
        " last_seen_ts = excluded.last_seen_ts, seeded = 1, updated_at = excluded.updated_at",
        (channel_id, last_seen_ts, now_iso),
    )


async def advance_cursor(
    conn: aiosqlite.Connection,
    *,
    channel_id: str,
    last_seen_ts: str,
    now_iso: str,
) -> None:
    """Move the high-water cursor forward to `last_seen_ts`. Used per emitted
    candidate (CASE 2) and to skip past pure chatter (CASE 3) / re-seed on a
    large gap (CASE 1b). Assumes the row already exists (post-seed)."""
    await conn.execute(
        "UPDATE slack_ci_alert_state SET last_seen_ts = ?, updated_at = ? WHERE channel_id = ?",
        (last_seen_ts, now_iso, channel_id),
    )


async def touch(
    conn: aiosqlite.Connection,
    *,
    channel_id: str,
    now_iso: str,
) -> None:
    """CASE 4: no new messages — update `updated_at` only (liveness signal)."""
    await conn.execute(
        "UPDATE slack_ci_alert_state SET updated_at = ? WHERE channel_id = ?",
        (now_iso, channel_id),
    )


async def list_cursors(conn: aiosqlite.Connection) -> list[CursorRow]:
    """All channel cursors, for `inspect ci-triage` / `ops doctor` liveness."""
    async with conn.execute(
        "SELECT channel_id, last_seen_ts, seeded, updated_at"
        " FROM slack_ci_alert_state ORDER BY channel_id"
    ) as cur:
        rows = await cur.fetchall()
    return [
        CursorRow(
            channel_id=str(r["channel_id"]),
            last_seen_ts=str(r["last_seen_ts"]),
            seeded=bool(r["seeded"]),
            updated_at=str(r["updated_at"]),
        )
        for r in rows
    ]


__all__ = [
    "CursorRow",
    "advance_cursor",
    "get_cursor",
    "list_cursors",
    "seed_cursor",
    "touch",
]
