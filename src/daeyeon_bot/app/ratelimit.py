"""Atomic token-bucket rate limiter persisted in SQLite.

`take(conn, bucket)` is the only sanctioned way to draw a token. It runs
the refill+decrement as a *single* atomic UPDATE so concurrent callers
race correctly via SQLite's row-level locking — this is the contract
required by `CONTRACTS.md §5`.

Bucket schema (migration 001):
    ratelimit_buckets(name TEXT PK, tokens REAL, capacity REAL,
                      refill_per_sec REAL, last_refill TEXT)

The refill is time-based and computed entirely in SQL:

    new_tokens = MIN(capacity, tokens + (now_unix - last_refill_unix)
                                          * refill_per_sec)

We use `unixepoch(?, 'subsec')` (REAL Unix-epoch seconds with sub-second
precision) rather than `julianday`. Reason: `julianday(t1) - julianday(t0)`
loses ~5e-6 days of precision per second of diff, which means a 1-second
elapsed reads as 0.99999 — under a `refill_per_sec=1.0` schedule the bucket
would never refill across an exact 1-second tick. `unixepoch(... ,'subsec')`
returns exact REAL seconds (verified on SQLite 3.50.4 bundled with Python
3.12), and was added in SQLite 3.42 — well below our minimum.

The dispatcher gates every claim on `take(self.db, "claude_call")` —
*before* `outbox.claim_one()`, parallel to the PAUSE check. This keeps
rate-limited cycles from incrementing `attempt` and tripping
`MAX_TRANSIENT_ATTEMPTS` on otherwise-healthy work (see
`docs/OPTIMIZATION_PLAN.md` §A3 for the rationale).
"""

from __future__ import annotations

import aiosqlite

# Default bucket consulted by the dispatcher before each claim.
CLAUDE_CALL_BUCKET = "claude_call"

# Atomic refill+decrement. Updates exactly when (and only when) the
# refilled token count would be ≥ 1.0; returns rowcount=1 on success.
# All four `?` carry the same `now_iso` so the math is consistent.
_TAKE_SQL = """
UPDATE ratelimit_buckets
   SET tokens = MIN(
           capacity,
           tokens + (unixepoch(?, 'subsec') - unixepoch(last_refill, 'subsec')) * refill_per_sec
       ) - 1,
       last_refill = ?
 WHERE name = ?
   AND MIN(
           capacity,
           tokens + (unixepoch(?, 'subsec') - unixepoch(last_refill, 'subsec')) * refill_per_sec
       ) >= 1.0
"""


async def take(conn: aiosqlite.Connection, bucket: str, *, now_iso: str) -> bool:
    """Atomically refill `bucket` and consume one token. Returns True iff
    a token was available after refill.

    A missing bucket returns False (rowcount=0), so a misconfigured caller
    fails closed rather than silently bypassing the limiter.

    Commits before returning so the take is durable per call — without this,
    the dispatcher's poll loop holds an open write transaction between polls
    and the WAL checkpoint at shutdown stalls on "database table is locked".
    """
    cursor = await conn.execute(_TAKE_SQL, (now_iso, now_iso, bucket, now_iso))
    try:
        result = cursor.rowcount == 1
    finally:
        await cursor.close()
    await conn.commit()
    return result


async def upsert_bucket(
    conn: aiosqlite.Connection,
    *,
    name: str,
    capacity: float,
    refill_per_sec: float,
    now_iso: str,
) -> None:
    """Idempotent bucket setup. Used at boot to apply config-driven knobs
    on top of the migration's seed values without resetting `tokens`.
    """
    await conn.execute(
        """
        INSERT INTO ratelimit_buckets (name, tokens, capacity, refill_per_sec, last_refill)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            capacity = excluded.capacity,
            refill_per_sec = excluded.refill_per_sec
        """,
        (name, capacity, capacity, refill_per_sec, now_iso),
    )


async def snapshot(
    conn: aiosqlite.Connection,
) -> list[tuple[str, float, float, float, str]]:
    """Read-only snapshot of every bucket. Returns (name, tokens, capacity,
    refill_per_sec, last_refill) tuples sorted by name. Used by `inspect
    ratelimit`.
    """
    async with conn.execute(
        "SELECT name, tokens, capacity, refill_per_sec, last_refill"
        " FROM ratelimit_buckets ORDER BY name"
    ) as cur:
        rows = await cur.fetchall()
    return [
        (
            str(r["name"]),
            float(r["tokens"]),
            float(r["capacity"]),
            float(r["refill_per_sec"]),
            str(r["last_refill"]),
        )
        for r in rows
    ]


__all__ = ["CLAUDE_CALL_BUCKET", "snapshot", "take", "upsert_bucket"]
