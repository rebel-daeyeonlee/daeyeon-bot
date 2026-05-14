"""Audit-row dataclass for the Jira triage feature.

Mirrors the shape of `daeyeon_bot.core.pr_review.audit.AuditRow` but adds
Jira-specific fields (parent_epic_key, hostname, tc_name, time_window,
collection-error labels, missing_fields). See
`specs/002-jira-triage-bot/data-model.md` §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One row of `jira_triage_audit` reconstructed from the DB."""

    id: int
    event_id: str
    issue_key: str
    parent_epic_key: str | None
    hostname: str | None
    tc_name: str | None
    branch: str | None
    head_sha: str | None
    run_id: str | None
    start_ts: datetime | None
    end_ts: datetime | None
    time_window_fallback: bool
    comment_seq: str
    status: str  # one of the CHECK enum values
    domain: str | None
    severity: str | None
    comment_id: str | None
    posted_at: datetime | None
    summary_chars: int | None
    evidence_count: int | None
    superseded_comment_ids: tuple[str, ...]
    loki_error: str | None
    ssh_error: str | None
    persona_skill: str | None
    persona_mtime_ns: int | None
    missing_fields: tuple[str, ...]
    error: str | None
    created_at: datetime


__all__ = ["AuditRow"]
