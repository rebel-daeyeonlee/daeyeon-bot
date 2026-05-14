"""Pure domain types for the Jira regression-failure triage feature.

Stdlib only — no I/O, no SDK, no third-party libraries. These dataclasses
flow from the polling trigger through the handler to Claude's user
message and back. See `specs/002-jira-triage-bot/data-model.md` §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Domain = Literal["Driver", "SysFw", "CpFw", "SysSol", "DevOps", "Connectivity", "unknown"]
Severity = Literal["sev1", "sev2", "sev3", "unknown"]
LokiStream = Literal["fwlog", "smclog", "kernel", "syslog"]
AssigneePath = Literal["user", "team", "manual"]


@dataclass(frozen=True, slots=True)
class TicketRef:
    """Identifying tuple for one Jira ticket."""

    project: str  # "SSWCI"
    issue_key: str  # "SSWCI-16787"
    created_iso: str  # ISO8601 UTC


@dataclass(frozen=True, slots=True)
class TitleParse:
    """Result of regex-parsing the ticket title.

    Title format: `regression-test . <hostname> . <TC-NNNN-...>`.
    """

    hostname: str  # "ssw-giga-02"
    tc_name: str  # "TC-0033-Dram_test_with_exception"


@dataclass(frozen=True, slots=True)
class EpicMeta:
    """Branch + commit from the parent Epic's custom fields."""

    epic_key: str
    branch: str
    commit: str  # 40-hex


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Loki query window. `fallback=True` when both bounds came from
    `created_at ± 30 min` rather than the ticket body's Start/End."""

    start_ts: datetime
    end_ts: datetime
    fallback: bool


@dataclass(frozen=True, slots=True)
class SshDumpLocation:
    """SSH log-dump location parsed from the ticket body."""

    host: str
    remote_path: str
    run_id: str  # "<digits>-<digits>"


@dataclass(frozen=True, slots=True)
class RunMeta:
    """Everything the handler resolved before collecting data."""

    ticket: TicketRef
    title: TitleParse
    epic: EpicMeta
    window: TimeWindow
    ssh: SshDumpLocation | None
    host_ip: str | None  # DNS-resolved; None when resolution failed


@dataclass(frozen=True, slots=True)
class LokiSlice:
    """One stream of Loki log lines for the run window."""

    stream: LokiStream
    lines: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SshArtifact:
    """One file fetched from the SSH log dump."""

    filename: str  # "output.xml"
    size_bytes: int
    contents: str | None  # None if oversized → skipped


@dataclass(frozen=True, slots=True)
class ProductCodeFile:
    """One source-file excerpt from `var/ssw-bundle/products/...`."""

    submodule_path: str  # "products/common/kmd"
    file_path: str  # repo-relative
    excerpt: str


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Everything the handler hands to Claude for a triage."""

    meta: RunMeta
    error_log_excerpt: str
    test_code: str | None
    product_code: tuple[ProductCodeFile, ...]
    loki_slices: tuple[LokiSlice, ...]
    ssh_artifacts: tuple[SshArtifact, ...]
    loki_error: str | None
    ssh_error: str | None


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One citation the persona used to ground its conclusion."""

    source: str  # "loki.fwlog" | "loki.smclog" | "loki.kernel" | "loki.syslog" | "ssh.<file>" | "test_code" | "product_code"
    quote: str
    citation: str  # "file:line" or ISO8601 timestamp


@dataclass(frozen=True, slots=True)
class SuspectedDuplicate:
    """One suspected duplicate ticket (best-effort, NOT verified by the bot)."""

    key: str  # "SSWCI-NNNN"
    basis: str


@dataclass(frozen=True, slots=True)
class TriageDraft:
    """Validated Claude output, ready to ship to Jira."""

    summary_md: str
    domain: Domain
    severity: Severity
    suspected_duplicates: tuple[SuspectedDuplicate, ...]
    needs_human: bool
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True, slots=True)
class PostedComment:
    """Result of a successful Jira `POST .../comment` call."""

    comment_id: str
    posted_at: datetime


__all__ = [
    "AssigneePath",
    "Domain",
    "EpicMeta",
    "EvidenceItem",
    "LokiSlice",
    "LokiStream",
    "PostedComment",
    "ProductCodeFile",
    "RunMeta",
    "RunSnapshot",
    "Severity",
    "SshArtifact",
    "SshDumpLocation",
    "SuspectedDuplicate",
    "TicketRef",
    "TimeWindow",
    "TitleParse",
    "TriageDraft",
]
