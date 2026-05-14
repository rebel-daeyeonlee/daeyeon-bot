"""CRUD for `jira_triage_audit` (data-model.md §1).

Append-mostly: one row per posted (or skipped / failed) triage attempt.
On force-supersede the existing row's `superseded_comment_ids` JSON
array gets the prior `comment_id` pushed; `comment_id`/`posted_at` are
then overwritten with the new values.

Mirrors `infra/pr_review_audit.py`'s shape; differs in column set
(Jira-specific fields like parent_epic_key, hostname, tc_name, etc.).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, cast

import aiosqlite

from daeyeon_bot.core.jira_triage.audit import AuditRow

AuditStatus = Literal[
    "posted",
    "skipped_not_regression_failure",
    "skipped_missing_metadata",
    "skipped_unresolvable_commit",
    "skipped_submodule_failure",
    "skipped_already_triaged",
    "failed",
]


async def insert_audit(
    conn: aiosqlite.Connection,
    *,
    event_id: str,
    issue_key: str,
    comment_seq: str,
    status: AuditStatus,
    created_at: datetime,
    parent_epic_key: str | None = None,
    hostname: str | None = None,
    tc_name: str | None = None,
    branch: str | None = None,
    head_sha: str | None = None,
    run_id: str | None = None,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
    time_window_fallback: bool = False,
    domain: str | None = None,
    severity: str | None = None,
    comment_id: str | None = None,
    posted_at: datetime | None = None,
    summary_chars: int | None = None,
    evidence_count: int | None = None,
    loki_error: str | None = None,
    ssh_error: str | None = None,
    persona_skill: str | None = None,
    persona_mtime_ns: int | None = None,
    missing_fields: tuple[str, ...] = (),
    error: str | None = None,
) -> int:
    """Insert one audit row; return the new `id`."""
    cursor = await conn.execute(
        "INSERT INTO jira_triage_audit("
        " event_id, issue_key, parent_epic_key, hostname, tc_name, branch,"
        " head_sha, run_id, start_ts, end_ts, time_window_fallback,"
        " comment_seq, status, domain, severity, comment_id, posted_at,"
        " summary_chars, evidence_count, loki_error, ssh_error,"
        " persona_skill, persona_mtime_ns, missing_fields, error, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            issue_key,
            parent_epic_key,
            hostname,
            tc_name,
            branch,
            head_sha,
            run_id,
            start_ts.isoformat() if start_ts is not None else None,
            end_ts.isoformat() if end_ts is not None else None,
            1 if time_window_fallback else 0,
            comment_seq,
            status,
            domain,
            severity,
            comment_id,
            posted_at.isoformat() if posted_at is not None else None,
            summary_chars,
            evidence_count,
            loki_error,
            ssh_error,
            persona_skill,
            persona_mtime_ns,
            json.dumps(list(missing_fields)),
            error,
            created_at.isoformat(),
        ),
    )
    new_id = cursor.lastrowid
    await cursor.close()
    if new_id is None:
        raise RuntimeError("INSERT INTO jira_triage_audit returned no rowid")
    return int(new_id)


async def find_latest(
    conn: aiosqlite.Connection,
    issue_key: str,
) -> AuditRow | None:
    """Return the most recent audit row for `issue_key`, or None."""
    async with conn.execute(
        _SELECT_COLS + " FROM jira_triage_audit WHERE issue_key = ? ORDER BY id DESC LIMIT 1",
        (issue_key,),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_audit(row) if row is not None else None


async def list_for_issue(
    conn: aiosqlite.Connection,
    *,
    issue_key: str,
    limit: int = 50,
) -> list[AuditRow]:
    """Audit rows for one issue, newest first. Used by `inspect jira-triage`."""
    async with conn.execute(
        _SELECT_COLS + " FROM jira_triage_audit WHERE issue_key = ? ORDER BY id DESC LIMIT ?",
        (issue_key, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_audit(r) for r in rows]


async def list_recent(
    conn: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[AuditRow]:
    """Most recent audit rows across every issue, newest first."""
    async with conn.execute(
        _SELECT_COLS + " FROM jira_triage_audit ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_audit(r) for r in rows]


async def record_supersede(
    conn: aiosqlite.Connection,
    audit_id: int,
    *,
    new_comment_id: str,
    new_posted_at: datetime,
) -> None:
    """Append existing row's `comment_id` to `superseded_comment_ids` and set new fields."""
    async with conn.execute(
        "SELECT comment_id, superseded_comment_ids FROM jira_triage_audit WHERE id = ?",
        (audit_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"jira_triage_audit row {audit_id} disappeared")
    old_comment_id_raw = row["comment_id"]
    raw_history = row["superseded_comment_ids"] or "[]"
    try:
        history_loaded: object = json.loads(raw_history)
    except (TypeError, ValueError):
        history_loaded = []
    history: list[str] = []
    if isinstance(history_loaded, list):
        history_items = cast("list[Any]", history_loaded)
        history = [str(item) for item in history_items]
    if old_comment_id_raw is not None:
        history.append(str(old_comment_id_raw))
    await conn.execute(
        "UPDATE jira_triage_audit SET comment_id = ?, posted_at = ?,"
        " superseded_comment_ids = ?, status = 'posted'"
        " WHERE id = ?",
        (
            new_comment_id,
            new_posted_at.isoformat(),
            json.dumps(history),
            audit_id,
        ),
    )


_SELECT_COLS = (
    "SELECT id, event_id, issue_key, parent_epic_key, hostname, tc_name,"
    " branch, head_sha, run_id, start_ts, end_ts, time_window_fallback,"
    " comment_seq, status, domain, severity, comment_id, posted_at,"
    " summary_chars, evidence_count, superseded_comment_ids, loki_error,"
    " ssh_error, persona_skill, persona_mtime_ns, missing_fields, error,"
    " created_at"
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


def _parse_datetime(raw: object) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(str(raw))


def _row_to_audit(row: aiosqlite.Row) -> AuditRow:
    return AuditRow(
        id=int(row["id"]),
        event_id=str(row["event_id"]),
        issue_key=str(row["issue_key"]),
        parent_epic_key=str(row["parent_epic_key"]) if row["parent_epic_key"] is not None else None,
        hostname=str(row["hostname"]) if row["hostname"] is not None else None,
        tc_name=str(row["tc_name"]) if row["tc_name"] is not None else None,
        branch=str(row["branch"]) if row["branch"] is not None else None,
        head_sha=str(row["head_sha"]) if row["head_sha"] is not None else None,
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        start_ts=_parse_datetime(row["start_ts"]),
        end_ts=_parse_datetime(row["end_ts"]),
        time_window_fallback=bool(row["time_window_fallback"]),
        comment_seq=str(row["comment_seq"]),
        status=str(row["status"]),
        domain=str(row["domain"]) if row["domain"] is not None else None,
        severity=str(row["severity"]) if row["severity"] is not None else None,
        comment_id=str(row["comment_id"]) if row["comment_id"] is not None else None,
        posted_at=_parse_datetime(row["posted_at"]),
        summary_chars=int(row["summary_chars"]) if row["summary_chars"] is not None else None,
        evidence_count=int(row["evidence_count"]) if row["evidence_count"] is not None else None,
        superseded_comment_ids=_parse_json_array(row["superseded_comment_ids"]),
        loki_error=str(row["loki_error"]) if row["loki_error"] is not None else None,
        ssh_error=str(row["ssh_error"]) if row["ssh_error"] is not None else None,
        persona_skill=str(row["persona_skill"]) if row["persona_skill"] is not None else None,
        persona_mtime_ns=int(row["persona_mtime_ns"])
        if row["persona_mtime_ns"] is not None
        else None,
        missing_fields=_parse_json_array(row["missing_fields"]),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


__all__ = [
    "AuditStatus",
    "find_latest",
    "insert_audit",
    "list_for_issue",
    "list_recent",
    "record_supersede",
]
