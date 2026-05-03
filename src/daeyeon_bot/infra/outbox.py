"""Outbox adapter — claim-row pattern for at-least-once delivery.

Three responsibilities:
    1. `insert(event, handler)` — append an event row (idempotent on the
       (source, source_dedup_key) UNIQUE) and one outbox row per handler.
    2. `claim_one(claimed_by)` — atomically grab the oldest pending row,
       returning its id + event for the dispatcher.
    3. `settle(id, result)` — transition the row to acked/retry/dead_letter,
       write a `runs` audit entry, and (for Ack) insert dedup_keys with TTL.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from daeyeon_bot.core.events import Event
from daeyeon_bot.core.results import Ack, DeadLetter, HandlerResult, Retry


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _payload_to_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), default=str, sort_keys=True)


def _payload_from_json(raw: str) -> Mapping[str, Any]:
    return json.loads(raw)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    outbox_id: int
    event: Event
    handler: str
    attempt: int
    attempt_epoch: int


async def insert_event(
    conn: aiosqlite.Connection,
    event: Event,
    *,
    source: str,
    source_dedup_key: str,
) -> bool:
    """Insert event row. Returns False if (source, source_dedup_key) already existed.

    The event is the *unit of arrival*; per-handler fanout happens via `enqueue_handler`.
    """
    try:
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.type,
                event.schema_version,
                source,
                source_dedup_key,
                _payload_to_json(event.payload),
                event.trace_id,
                _to_iso(event.created_at),
            ),
        )
    except aiosqlite.IntegrityError:
        return False
    return True


async def enqueue_handler(
    conn: aiosqlite.Connection,
    *,
    event_id: str,
    handler: str,
    now: datetime,
    attempt_epoch: int = 0,
) -> int:
    """Insert one outbox row, returning the rowid."""
    cursor = await conn.execute(
        "INSERT INTO outbox(event_id, handler, status, attempt, attempt_epoch,"
        " created_at, updated_at)"
        " VALUES (?, ?, 'pending', 0, ?, ?, ?)",
        (event_id, handler, attempt_epoch, _to_iso(now), _to_iso(now)),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


async def claim_one(
    conn: aiosqlite.Connection,
    *,
    claimed_by: str,
    now: datetime,
) -> ClaimedJob | None:
    """Atomically claim the oldest eligible outbox row.

    Eligibility:
        - status='pending', or
        - status='retry' AND next_attempt_at <= now.

    Atomicity:
        UPDATE … SET claimed_by=?, status='running' WHERE id=(SELECT id …) AND claimed_by IS NULL.
        If two dispatchers race, only one rowcount=1.
    """
    iso_now = _to_iso(now)
    # Select candidate id outside the UPDATE so we can return event details after claim.
    async with conn.execute(
        """
        SELECT id, event_id, handler, attempt, attempt_epoch
          FROM outbox
         WHERE claimed_by IS NULL
           AND (
                status = 'pending'
             OR (status = 'retry' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
           )
         ORDER BY id
         LIMIT 1
        """,
        (iso_now,),
    ) as cur:
        candidate = await cur.fetchone()
    if candidate is None:
        return None

    cursor = await conn.execute(
        """
        UPDATE outbox
           SET claimed_by = ?, claimed_at = ?, status = 'running', updated_at = ?
         WHERE id = ? AND claimed_by IS NULL
        """,
        (claimed_by, iso_now, iso_now, candidate["id"]),
    )
    if cursor.rowcount != 1:
        await conn.commit()
        return None
    await conn.commit()

    async with conn.execute(
        "SELECT id, type, schema_version, payload_json, trace_id, created_at"
        " FROM events WHERE id = ?",
        (candidate["event_id"],),
    ) as cur:
        ev_row = await cur.fetchone()
    assert ev_row is not None, "outbox references missing event row"

    event = Event(
        id=ev_row["id"],
        type=ev_row["type"],
        schema_version=int(ev_row["schema_version"]),
        payload=_payload_from_json(ev_row["payload_json"]),
        trace_id=ev_row["trace_id"],
        created_at=datetime.fromisoformat(ev_row["created_at"]),
    )
    return ClaimedJob(
        outbox_id=int(candidate["id"]),
        event=event,
        handler=candidate["handler"],
        attempt=int(candidate["attempt"]),
        attempt_epoch=int(candidate["attempt_epoch"]),
    )


async def settle(
    conn: aiosqlite.Connection,
    *,
    job: ClaimedJob,
    result: HandlerResult,
    started_at: datetime,
    finished_at: datetime,
    dedup_ttl: timedelta | None,
) -> None:
    """Apply the handler's result to outbox + runs + (Ack only) dedup_keys."""
    iso_finished = _to_iso(finished_at)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    match result:
        case Ack():
            new_status = "acked"
            error_text: str | None = None
            await conn.execute(
                """
                UPDATE outbox
                   SET status = ?, claimed_by = NULL, claimed_at = NULL,
                       attempt = attempt + 1, last_error = NULL,
                       next_attempt_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (new_status, iso_finished, job.outbox_id),
            )
            if dedup_ttl is not None:
                key = f"{job.event.id}:{job.handler}:{job.attempt_epoch}"
                expires = finished_at + dedup_ttl
                await conn.execute(
                    "INSERT OR REPLACE INTO dedup_keys(key, expires_at) VALUES (?, ?)",
                    (key, _to_iso(expires)),
                )
        case Retry(after_s=after):
            new_status = "retry"
            error_text = None
            next_at = finished_at + timedelta(seconds=float(after))
            await conn.execute(
                """
                UPDATE outbox
                   SET status = ?, claimed_by = NULL, claimed_at = NULL,
                       attempt = attempt + 1, last_error = NULL,
                       next_attempt_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (new_status, _to_iso(next_at), iso_finished, job.outbox_id),
            )
        case DeadLetter(reason=reason):
            new_status = "dead_letter"
            error_text = reason
            await conn.execute(
                """
                UPDATE outbox
                   SET status = ?, claimed_by = NULL, claimed_at = NULL,
                       attempt = attempt + 1, last_error = ?,
                       next_attempt_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (new_status, reason, iso_finished, job.outbox_id),
            )

    await conn.execute(
        "INSERT INTO runs(outbox_id, event_id, handler, attempt_epoch, started_at,"
        " finished_at, status, duration_ms, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job.outbox_id,
            job.event.id,
            job.handler,
            job.attempt_epoch,
            _to_iso(started_at),
            iso_finished,
            new_status,
            duration_ms,
            error_text,
        ),
    )
    await conn.commit()


async def is_deduped(
    conn: aiosqlite.Connection, *, event_id: str, handler: str, attempt_epoch: int, now: datetime
) -> bool:
    """Return True if this (event, handler, attempt_epoch) has a non-expired dedup key."""
    key = f"{event_id}:{handler}:{attempt_epoch}"
    async with conn.execute("SELECT expires_at FROM dedup_keys WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    return datetime.fromisoformat(row["expires_at"]) > now


async def mark_interrupted(
    conn: aiosqlite.Connection,
    *,
    outbox_ids: list[int],
    now: datetime,
    reason: str = "interrupted",
) -> int:
    """Mark a set of outbox rows as 'interrupted'. Caller already commits.

    Used by the dispatcher's drain timeout path to record stragglers; the next
    boot's `recover_interrupted_rows` decides whether they retry or DLQ.
    """
    if not outbox_ids:
        return 0
    iso_now = _to_iso(now)
    placeholders = ",".join("?" * len(outbox_ids))
    cursor = await conn.execute(
        f"""
        UPDATE outbox
           SET status = 'interrupted', last_error = ?, claimed_by = NULL, claimed_at = NULL,
               updated_at = ?
         WHERE id IN ({placeholders}) AND status = 'running'
        """,  # noqa: S608 — placeholders are integer ids built above, not user input
        (reason, iso_now, *outbox_ids),
    )
    await conn.commit()
    return cursor.rowcount


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    crashed: int  # rows that were 'running' on boot — daemon was killed mid-flight
    rerun: int  # 'interrupted' rows promoted back to 'pending' (idempotent handlers)
    dead_lettered: int  # 'interrupted' rows sent to DLQ (non-idempotent or unknown handler)


async def recover_interrupted_rows(
    conn: aiosqlite.Connection,
    *,
    idempotent_handlers: set[str],
    known_handlers: set[str],
    now: datetime,
) -> RecoveryReport:
    """Boot-time recovery for in-flight rows from a prior unclean shutdown.

    Two passes:
    1. Any `status='running'` row is from a crashed/killed daemon. Demote to
       `'interrupted'` so step 2 can decide its fate uniformly.
    2. For every `status='interrupted'` row, look up the handler:
       - idempotent → `'pending'` (will be re-claimed and rerun)
       - non-idempotent or handler unknown → `'dead_letter'`

    Caller is responsible for ensuring this runs *before* the dispatcher starts
    polling, otherwise rows can be claimed before recovery decides their fate.
    """
    iso_now = _to_iso(now)
    crashed_cur = await conn.execute(
        """
        UPDATE outbox
           SET status = 'interrupted',
               last_error = COALESCE(last_error, 'daemon crash or kill -9'),
               claimed_by = NULL, claimed_at = NULL, updated_at = ?
         WHERE status = 'running'
        """,
        (iso_now,),
    )
    crashed_count = crashed_cur.rowcount

    async with conn.execute("SELECT id, handler FROM outbox WHERE status = 'interrupted'") as cur:
        rows = await cur.fetchall()

    rerun_ids: list[int] = []
    dlq_pairs: list[tuple[int, str]] = []
    for row in rows:
        handler = row["handler"]
        if handler in idempotent_handlers:
            rerun_ids.append(int(row["id"]))
        elif handler in known_handlers:
            dlq_pairs.append((int(row["id"]), "interrupted (non-idempotent handler)"))
        else:
            dlq_pairs.append((int(row["id"]), "interrupted (handler not registered)"))

    if rerun_ids:
        placeholders = ",".join("?" * len(rerun_ids))
        await conn.execute(
            f"""
            UPDATE outbox
               SET status = 'pending', last_error = NULL, next_attempt_at = NULL,
                   updated_at = ?
             WHERE id IN ({placeholders})
            """,  # noqa: S608 — placeholders are integer ids
            (iso_now, *rerun_ids),
        )
    for outbox_id, reason in dlq_pairs:
        await conn.execute(
            """
            UPDATE outbox
               SET status = 'dead_letter', last_error = ?, updated_at = ?
             WHERE id = ?
            """,
            (reason, iso_now, outbox_id),
        )
    await conn.commit()
    return RecoveryReport(crashed=crashed_count, rerun=len(rerun_ids), dead_lettered=len(dlq_pairs))
