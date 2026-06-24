"""CRUD for `ci_triage_audit` (migration 006).

Append-only: one row per posted (or skipped / failed) triage. ci_triage's
force-supersede posts a *new* Slack message under a new event/dedup token (the
Slack adapter has no `chat.update`), so there is no in-place supersede update —
each force is simply a new audit row. Mirrors `infra/jira_triage_audit.py`'s
shape, adapted to the ci_triage column set.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, cast

import aiosqlite

from daeyeon_bot.core.ci_triage.types import AuditRow

AuditStatus = Literal[
    "posted",
    "skipped_no_run_link",
    "skipped_not_ci_failure",
    "skipped_already_triaged",
    "skipped_log_unavailable",
    "failed",
]


async def insert_audit(
    conn: aiosqlite.Connection,
    *,
    event_id: str,
    channel_id: str,
    message_ts: str,
    status: AuditStatus,
    created_at: datetime,
    repo: str | None = None,
    run_id: str | None = None,
    pr_number: int | None = None,
    failed_jobs: tuple[str, ...] = (),
    attribution: str | None = None,
    classification: str | None = None,
    owner_area: str | None = None,
    confidence: str | None = None,
    wiki_matches: tuple[str, ...] = (),
    posted_channel_id: str | None = None,
    posted_message_ts: str | None = None,
    summary_chars: int | None = None,
    persona_skill: str | None = None,
    persona_mtime_ns: int | None = None,
    gh_error: str | None = None,
    wiki_error: str | None = None,
    error: str | None = None,
    dut_host: str | None = None,
    signature: str | None = None,
) -> int:
    """Insert one audit row; return the new `id`."""
    cursor = await conn.execute(
        "INSERT INTO ci_triage_audit("
        " event_id, channel_id, message_ts, repo, run_id, pr_number, failed_jobs,"
        " status, attribution, classification, owner_area, confidence, wiki_matches,"
        " posted_channel_id, posted_message_ts, summary_chars, persona_skill,"
        " persona_mtime_ns, gh_error, wiki_error, error, dut_host, signature, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            channel_id,
            message_ts,
            repo,
            run_id,
            pr_number,
            json.dumps(list(failed_jobs)),
            status,
            attribution,
            classification,
            owner_area,
            confidence,
            json.dumps(list(wiki_matches)),
            posted_channel_id,
            posted_message_ts,
            summary_chars,
            persona_skill,
            persona_mtime_ns,
            gh_error,
            wiki_error,
            error,
            dut_host,
            signature,
            created_at.isoformat(),
        ),
    )
    new_id = cursor.lastrowid
    await cursor.close()
    if new_id is None:
        raise RuntimeError("INSERT INTO ci_triage_audit returned no rowid")
    return int(new_id)


async def find_posted_for_message(
    conn: aiosqlite.Connection,
    *,
    channel_id: str,
    message_ts: str,
) -> AuditRow | None:
    """Primary idempotency guard: a posted row for this exact alert message."""
    async with conn.execute(
        _SELECT_COLS + " FROM ci_triage_audit"
        " WHERE channel_id = ? AND message_ts = ? AND status = 'posted'"
        " ORDER BY id DESC LIMIT 1",
        (channel_id, message_ts),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_audit(row) if row is not None else None


async def find_posted_for_run(
    conn: aiosqlite.Connection,
    *,
    repo: str,
    run_id: str,
) -> AuditRow | None:
    """Secondary cross-alert guard: a posted row for this run (repo, run_id).

    Correctness depends on `concurrency=1` (the ci_triage manifest) — this is an
    audit *lookup*, not a SQL UNIQUE constraint, so two events for the same run
    only collapse because concurrency=1 serializes claims. NULL (repo, run_id)
    rows never match (SQL NULL = NULL is never true), which is intended: two
    link-less alerts are two independent triages.
    """
    async with conn.execute(
        _SELECT_COLS + " FROM ci_triage_audit"
        " WHERE repo = ? AND run_id = ? AND status = 'posted'"
        " ORDER BY id DESC LIMIT 1",
        (repo, run_id),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_audit(row) if row is not None else None


async def find_latest_for_message(
    conn: aiosqlite.Connection,
    *,
    channel_id: str,
    message_ts: str,
) -> AuditRow | None:
    """Most recent audit row (any status) for one alert message. For `inspect`."""
    async with conn.execute(
        _SELECT_COLS + " FROM ci_triage_audit"
        " WHERE channel_id = ? AND message_ts = ? ORDER BY id DESC LIMIT 1",
        (channel_id, message_ts),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_audit(row) if row is not None else None


async def count_recent_by_signature(
    conn: aiosqlite.Connection,
    *,
    signature: str,
    since_iso: str,
    exclude_message_ts: str | None = None,
) -> int:
    """Count `posted` triages with this `signature` since `since_iso` (P2
    recurrence). Excludes the current alert's message_ts so re-triage of the
    same alert doesn't self-count. Empty signature → 0."""
    if not signature:
        return 0
    sql = (
        "SELECT COUNT(*) AS n FROM ci_triage_audit"
        " WHERE signature = ? AND status = 'posted' AND created_at >= ?"
    )
    params: list[Any] = [signature, since_iso]
    if exclude_message_ts is not None:
        sql += " AND message_ts != ?"
        params.append(exclude_message_ts)
    async with conn.execute(sql, tuple(params)) as cursor:
        row = await cursor.fetchone()
    return int(row["n"]) if row is not None else 0


async def list_recent(
    conn: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[AuditRow]:
    """Most recent audit rows across every channel, newest first."""
    async with conn.execute(
        _SELECT_COLS + " FROM ci_triage_audit ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_audit(r) for r in rows]


_SELECT_COLS = (
    "SELECT id, event_id, channel_id, message_ts, repo, run_id, pr_number,"
    " failed_jobs, status, attribution, classification, owner_area, confidence,"
    " wiki_matches, posted_channel_id, posted_message_ts, summary_chars,"
    " persona_skill, persona_mtime_ns, gh_error, wiki_error, error, created_at"
)


def _parse_json_array(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        loaded: object = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(loaded, list):
        return ()
    items = cast("list[Any]", loaded)
    return tuple(str(x) for x in items)


def _row_to_audit(row: aiosqlite.Row) -> AuditRow:
    return AuditRow(
        id=int(row["id"]),
        event_id=str(row["event_id"]),
        channel_id=str(row["channel_id"]),
        message_ts=str(row["message_ts"]),
        repo=str(row["repo"]) if row["repo"] is not None else None,
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
        failed_jobs=_parse_json_array(row["failed_jobs"]),
        status=str(row["status"]),
        attribution=str(row["attribution"]) if row["attribution"] is not None else None,
        classification=str(row["classification"]) if row["classification"] is not None else None,
        owner_area=str(row["owner_area"]) if row["owner_area"] is not None else None,
        confidence=str(row["confidence"]) if row["confidence"] is not None else None,
        wiki_matches=_parse_json_array(row["wiki_matches"]),
        posted_channel_id=str(row["posted_channel_id"])
        if row["posted_channel_id"] is not None
        else None,
        posted_message_ts=str(row["posted_message_ts"])
        if row["posted_message_ts"] is not None
        else None,
        summary_chars=int(row["summary_chars"]) if row["summary_chars"] is not None else None,
        persona_skill=str(row["persona_skill"]) if row["persona_skill"] is not None else None,
        persona_mtime_ns=int(row["persona_mtime_ns"])
        if row["persona_mtime_ns"] is not None
        else None,
        gh_error=str(row["gh_error"]) if row["gh_error"] is not None else None,
        wiki_error=str(row["wiki_error"]) if row["wiki_error"] is not None else None,
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


__all__ = [
    "AuditStatus",
    "count_recent_by_signature",
    "find_latest_for_message",
    "find_posted_for_message",
    "find_posted_for_run",
    "insert_audit",
    "list_recent",
]
