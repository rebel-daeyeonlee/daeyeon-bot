"""Pure regex parsers for the jira_triage handler.

All parsers return `None` (or a sentinel `()` / `""`) on miss; the
handler decides whether to skip the triage (title miss → audit
`skipped_not_regression_failure`) or fall back (timestamps missing →
widen Loki window to `created_at ± 30 min` per FR-006).

Stdlib only.
"""

from __future__ import annotations

import re
from datetime import datetime

from daeyeon_bot.core.jira_triage.types import SshDumpLocation, TitleParse

# ── Title (FR-008) ────────────────────────────────────────────────────────────

_TITLE_RE = re.compile(
    r"^regression-test\s*\.\s*"
    r"(?P<hostname>[\w.-]+)\s*\.\s*"
    r"(?P<tc>TC-\d+-\S+)\s*$"
)


def parse_title(summary: str) -> TitleParse | None:
    """Title format: `regression-test . <hostname> . <TC-NNNN-...>`."""
    match = _TITLE_RE.match(summary or "")
    if match is None:
        return None
    return TitleParse(
        hostname=match.group("hostname"),
        tc_name=match.group("tc"),
    )


# ── Start/End timestamps (FR-006) ─────────────────────────────────────────────

_TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\.\d{1,6})")


def parse_timestamps(body_text: str) -> tuple[datetime, datetime] | None:
    """Extract the first two matching timestamps from the ticket body.

    Matches `2026-05-13 06:54:48.924242` (with space) and
    `2026-05-13T06:54:48.924242` (with T). Microsecond precision optional
    (1-6 digits accepted). Returns `(start, end)` as **naive datetimes**
    in the ticket's local convention (the bot's caller is expected to
    treat them as UTC unless documented otherwise — Loki's `query_range`
    converts to ns timestamps via `datetime.timestamp()` which respects
    the tzinfo).

    On a single match or zero matches, returns None — caller falls back
    to `created_at ± 30 min`.
    """
    if not body_text:
        return None
    matches = _TS_RE.findall(body_text)
    if len(matches) < 2:
        return None
    try:
        start = _parse_ts(matches[0])
        end = _parse_ts(matches[1])
    except ValueError:
        return None
    if end <= start:
        return None
    return (start, end)


def _parse_ts(raw: str) -> datetime:
    # Accept both `T` and ` ` separators; pad sub-second precision.
    normalized = raw.replace(" ", "T")
    # `fromisoformat` requires exactly 3 or 6 digits of sub-seconds in 3.12.
    if "." in normalized:
        head, frac = normalized.split(".", 1)
        # Pad to 6 digits or truncate.
        frac = (frac + "000000")[:6]
        normalized = f"{head}.{frac}"
    return datetime.fromisoformat(normalized)


# ── SSH URL (FR-007) ──────────────────────────────────────────────────────────

_SSH_URL_RE = re.compile(
    r"ssh://automation@(?P<host>[\w.-]+):"
    r"(?P<path>/mnt/data/logs/regression-test/"
    r"(?P<run_id>[\d\-]+)/"
    r"(?P<host2>[\w.-]+)/"
    # `[\w-]+` stops at `)` so we don't slurp the trailing markdown-link
    # delimiter when the URL is embedded as `[Link](ssh://...)`.
    r"(?P<tc>TC-\d+-[\w-]+))"
)


def parse_ssh_url(body_text: str) -> SshDumpLocation | None:
    """Extract the canonical SSH log-dump URL from the ticket body."""
    if not body_text:
        return None
    match = _SSH_URL_RE.search(body_text)
    if match is None:
        return None
    return SshDumpLocation(
        host=match.group("host"),
        remote_path=match.group("path"),
        run_id=match.group("run_id"),
    )


# ── Error-log excerpt ─────────────────────────────────────────────────────────

_NOFORMAT_RE = re.compile(r"\{noformat\}(?P<body>.*?)\{noformat\}", re.DOTALL)
_ERROR_LOG_MAX = 4096


def extract_error_log(body_text: str) -> str:
    """Extract the most informative chunk from the ticket body.

    Strategy:
      1. If the body contains a `{noformat}...{noformat}` block, return
         that block's contents (this is the wiki-markup convention
         ssw-bundle's `jira_bug.py` uses for stack traces).
      2. Otherwise, return the first `_ERROR_LOG_MAX` chars of the body.

    Returns empty string when the body itself is empty.
    """
    if not body_text:
        return ""
    matches = _NOFORMAT_RE.findall(body_text)
    if matches:
        # Pick the longest noformat block — most likely the real stack trace.
        best = max(matches, key=len)
        return best.strip()[:_ERROR_LOG_MAX]
    return body_text.strip()[:_ERROR_LOG_MAX]


__all__ = [
    "extract_error_log",
    "parse_ssh_url",
    "parse_timestamps",
    "parse_title",
]
