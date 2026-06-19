"""Value objects passed between the ci_triage adapters and handler.

Pure dataclasses (stdlib only). The Claude output model (`TriageOutput`) is a
Pydantic v2 model and lives in `handlers/ci_triage_schemas.py` (pydantic is an
infra/handler-layer dependency, not a core one). The audit-row reconstruction
dataclass lives here, mirroring `core/jira_triage/audit.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RunRef:
    """A GitHub Actions run reference extracted from an alert."""

    repo: str  # "rebellions-sw/ssw-bundle"
    run_id: str  # numeric id as string, from .../actions/runs/<id>


@dataclass(frozen=True, slots=True)
class LokiWindow:
    """A host + time window pulled from a Grafana/Loki link in an alert.

    Feeds the device-level dual-evidence path when a run log is absent or
    insufficient. `start`/`end` are ISO8601 UTC strings as parsed from the link.
    """

    host: str
    start: str | None = None
    end: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedAlert:
    """The machine-readable facts extracted from one Slack alert message.

    `merged_text` is `text` + every `attachments[].{title,text,fallback}` +
    `blocks[].text` concatenated (SSW-Alert-Bot content lives only in
    `attachments[].text`). The handler re-parses this deterministically rather
    than re-fetching from Slack.
    """

    channel_id: str
    message_ts: str
    author_id: str | None
    merged_text: str
    run_ref: RunRef | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    failed_jobs: tuple[str, ...] = ()
    consecutive_fail_count: int | None = None
    loki_window: LokiWindow | None = None


@dataclass(frozen=True, slots=True)
class FailedLog:
    """An error-anchored, ANSI-stripped, redacted slice of a `--log-failed` dump."""

    run_ref: RunRef
    raw_chars: int  # size of the original dump (for audit / observability)
    anchored_text: str  # the error-anchored windows handed to Claude
    failed_jobs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WikiMatch:
    """One OnCall-wiki incident / playbook match for the failure signature."""

    path: str  # vault-relative path, e.g. "wiki/oncall/incidents/qemu-golden-base-image-missing.md"
    signature_matched: bool  # True when matched against the incident `signature:` frontmatter
    score: int  # higher = stronger (multi-word phrase > lone token)
    snippet: str


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One row of `ci_triage_audit` reconstructed from the DB.

    Mirrors `core/jira_triage/audit.py:AuditRow`, adapted to the ci_triage
    column set (see migration 006).
    """

    id: int
    event_id: str
    channel_id: str
    message_ts: str
    repo: str | None
    run_id: str | None
    pr_number: int | None
    failed_jobs: tuple[str, ...]
    status: str  # one of the CHECK enum values
    attribution: str | None
    classification: str | None
    owner_area: str | None
    confidence: str | None
    wiki_matches: tuple[str, ...]
    posted_channel_id: str | None
    posted_message_ts: str | None
    summary_chars: int | None
    persona_skill: str | None
    persona_mtime_ns: int | None
    gh_error: str | None
    wiki_error: str | None
    error: str | None
    created_at: datetime


__all__ = [
    "AuditRow",
    "FailedLog",
    "LokiWindow",
    "ParsedAlert",
    "RunRef",
    "WikiMatch",
]
