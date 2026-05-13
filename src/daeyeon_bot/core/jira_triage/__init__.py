"""Pure domain types for the Jira regression-failure triage feature.

This package is stdlib-only — no I/O, no SDK, no third-party libraries.
Public types are re-exported from this module so callers import via
`from daeyeon_bot.core.jira_triage import <Type>`.

Types are populated incrementally as the feature implementation lands:
- T011: TicketRef, TitleParse, EpicMeta, TimeWindow, SshDumpLocation,
        RunMeta, LokiSlice, SshArtifact, ProductCodeFile, RunSnapshot,
        EvidenceItem, SuspectedDuplicate, TriageDraft, PostedComment
- T012: AuditRow
"""

from __future__ import annotations
