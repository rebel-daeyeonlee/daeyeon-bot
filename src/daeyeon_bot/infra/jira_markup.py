"""Jira wiki-markup helpers for the triage comment body.

The bot posts comments via REST v2 (`POST /rest/api/2/issue/{key}/comment`)
which accepts a plain-string `body` in Jira wiki markup. This module
generates that body deterministically from a `TriageDraft`. Wiki-markup
dialect matches `ssw-bundle/inv/test_report/jira_markup.py` conventions
(`*bold*`, `h3.`, `{noformat}`, `{quote}`, `{{code}}`).

Pure functions, stdlib only.
"""

from __future__ import annotations

from daeyeon_bot.core.jira_triage.types import (
    EvidenceItem,
    SuspectedDuplicate,
    TriageDraft,
)


def h3(title: str) -> str:
    """`h3.` heading. Jira wiki: leading `h3. ` on its own line."""
    return f"h3. {title}"


def bullet(text: str) -> str:
    """Single bullet line. Jira wiki: leading `* `."""
    return f"* {text}"


def code(text: str) -> str:
    """Inline `{{...}}` code span. Escapes `}}` if it sneaks in."""
    safe = text.replace("}}", "} }")
    return "{{" + safe + "}}"


def noformat(text: str) -> str:
    """`{noformat}…{noformat}` block. For long quoted log lines."""
    return "{noformat}\n" + text + "\n{noformat}"


def quote(text: str) -> str:
    """`{quote}…{quote}` block. Renders as a callout."""
    return "{quote}" + text + "{quote}"


def bold(text: str) -> str:
    """`*bold*` inline. Doesn't escape — caller is responsible for nesting."""
    return f"*{text}*"


def build_comment(triage: TriageDraft, *, supersede_header: str | None = None) -> str:
    """Assemble the 4-section comment body.

    Sections, in order:
      1. (optional) supersede header in `{quote}…{quote}`
      2. h3. Symptom
      3. h3. Evidence cited
      4. h3. Likely layer
      5. h3. Next data to collect
      6. (optional) suspected duplicates block

    `summary_md` (from `TriageDraft`) is the full 4-section body Claude
    produced. We trust that and ship it as-is — wiki-markup converters
    are out of scope; the persona is told to emit Jira wiki markup
    directly in the 4-section template.
    """
    parts: list[str] = []

    if supersede_header:
        parts.append(quote(supersede_header))
        parts.append("")  # blank line

    # Body. The persona's `summary_md` already contains the 4 h3. headings.
    parts.append(triage.summary_md.rstrip())

    if triage.suspected_duplicates:
        parts.append("")
        parts.append(h3("Suspected duplicates (best-effort, NOT verified)"))
        for dup in triage.suspected_duplicates:
            parts.append(bullet(f"{bold(dup.key)} — {dup.basis}"))

    if triage.needs_human:
        parts.append("")
        parts.append(quote("needs_human=true — operator review required."))

    return "\n".join(parts) + "\n"


def supersede_header_text(prior_posted_at_hhmmss_utc: str) -> str:
    """Standard supersede-header text used as the leading `{quote}` block."""
    return (
        f"Updated triage (supersedes earlier bot comment posted at {prior_posted_at_hhmmss_utc})."
    )


def evidence_bullet(item: EvidenceItem) -> str:
    """Helper for callers that want to render a single Evidence item.

    Format: `* <source> @ <citation> — {{<quote>}}`.
    Long quotes (>200 chars) use `{noformat}` instead of `{{...}}`.
    """
    if len(item.quote) > 200 or "\n" in item.quote:
        body = noformat(item.quote)
        return f"{bullet(f'{item.source} @ {item.citation} —')}\n{body}"
    return bullet(f"{item.source} @ {item.citation} — {code(item.quote)}")


def duplicate_bullet(dup: SuspectedDuplicate) -> str:
    """Helper for callers that want to render a single suspected duplicate."""
    return bullet(f"{bold(dup.key)} — {dup.basis}")


__all__ = [
    "bold",
    "build_comment",
    "bullet",
    "code",
    "duplicate_bullet",
    "evidence_bullet",
    "h3",
    "noformat",
    "quote",
    "supersede_header_text",
]
