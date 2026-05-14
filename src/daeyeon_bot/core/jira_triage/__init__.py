"""Pure domain types for the Jira regression-failure triage feature.

Stdlib only — no I/O, no SDK, no third-party libraries. Public types are
re-exported below so callers import via
`from daeyeon_bot.core.jira_triage import <Type>`.
"""

from __future__ import annotations

from daeyeon_bot.core.jira_triage.audit import AuditRow
from daeyeon_bot.core.jira_triage.types import (
    AssigneePath,
    Domain,
    EpicMeta,
    EvidenceItem,
    LokiSlice,
    LokiStream,
    PostedComment,
    ProductCodeFile,
    RunMeta,
    RunSnapshot,
    Severity,
    SshArtifact,
    SshDumpLocation,
    SuspectedDuplicate,
    TicketRef,
    TimeWindow,
    TitleParse,
    TriageDraft,
)

__all__ = [
    "AssigneePath",
    "AuditRow",
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
