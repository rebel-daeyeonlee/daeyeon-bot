"""Pure parsing of Slack CI-failure alert messages (feature 003).

Three alert sources, three text locations (verified live 2026-06-19):
  - `sukju-bot` (#help): top-level `text`, structured bullets (PR / head SHA /
    실패 job / 실패 run / 연속 실패 N회).
  - `dev_syssw_test` (#alerts): top-level `text` mrkdwn (`*Workflow:* <run|Run
    Link>`, `*Logs:* <grafana-loki|syslog>`, `[<host>]` in the title line).
  - `SSW-Alert-Bot` (#alerts): Grafana alerting → Slack, content ONLY in
    `attachments[].{title,text,fallback}` — empty top-level `text`.

So `merge_message_text` concatenates `text` + every attachment's title/text/
fallback + block text; extraction regexes run over the merged blob. No I/O.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, cast
from urllib.parse import unquote

from daeyeon_bot.core.ci_triage.types import LokiWindow, ParsedAlert, RunRef

# SSW DUT/runner hostnames as they appear in job logs (e.g. "ssw-host-04",
# "ssw-smci-16"). Used to find the device-under-test host for a Loki query when
# the runner is a controller (ssw-hp-01) distinct from the DUT (ssw-host-04).
_DUT_HOST_RE = re.compile(r"\bssw-(?:host|smci|giga|pc|arm|rebel)-\d+\b", re.IGNORECASE)

# github.com/<owner>/<repo>/actions/runs/<id>[/job/<job_id>]
_RUN_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/(\d+)",
)
_PR_RE = re.compile(r"/pull/(\d+)\b|(?:^|\s)#(\d+)\b|PR[^\n]*?#(\d+)\b")
_HEAD_SHA_RE = re.compile(r"head\s*sha[:\s]*([0-9a-f]{7,40})", re.IGNORECASE)
_FAILED_JOB_RE = re.compile(r"실패\s*job[:\s]*([^\n]+)", re.IGNORECASE)
_CONSEC_RE = re.compile(r"연속\s*실패[:\s]*(\d+)\s*회", re.IGNORECASE)
# `[ssw-smci-16]` host tag in dev_syssw_test / a hostname=~"host" in a Loki link.
_HOST_BRACKET_RE = re.compile(r"\[((?:ssw|host)[A-Za-z0-9_-]+)\]")
_HOST_LOKI_RE = re.compile(r'hostname["%]*\s*[=~]+\s*[\\"%]*([A-Za-z0-9_-]+)')
# Grafana explore range "from":"<ms>","to":"<ms>" (URL-encoded in the link).
_RANGE_FROM_RE = re.compile(r'"from"\s*:\s*"(\d{10,})"')
_RANGE_TO_RE = re.compile(r'"to"\s*:\s*"(\d{10,})"')


def _block_text(block: dict[str, Any]) -> list[str]:
    """Pull readable strings out of one Block Kit block (best-effort)."""
    out: list[str] = []
    text = block.get("text")
    if isinstance(text, str):
        out.append(text)
    elif isinstance(text, dict):
        inner = cast("dict[str, Any]", text).get("text")
        if isinstance(inner, str):
            out.append(inner)
    fields = block.get("fields")
    if isinstance(fields, list):
        for f in cast("list[Any]", fields):
            if isinstance(f, dict):
                ft = cast("dict[str, Any]", f).get("text")
                if isinstance(ft, str):
                    out.append(ft)
    return out


def merge_message_text(msg: dict[str, Any]) -> str:
    """Concatenate top-level `text` + every `attachments[].{title,text,fallback}`
    + `blocks[].text`. All three locations, because SSW-Alert-Bot content lives
    only in `attachments[].text`."""
    parts: list[str] = []
    top = msg.get("text")
    if isinstance(top, str) and top:
        parts.append(top)

    attachments = msg.get("attachments")
    if isinstance(attachments, list):
        for att in cast("list[Any]", attachments):
            if not isinstance(att, dict):
                continue
            att_d = cast("dict[str, Any]", att)
            for key in ("title", "text", "fallback"):
                val = att_d.get(key)
                if isinstance(val, str) and val:
                    parts.append(val)

    blocks = msg.get("blocks")
    if isinstance(blocks, list):
        for block in cast("list[Any]", blocks):
            if isinstance(block, dict):
                parts.extend(_block_text(cast("dict[str, Any]", block)))

    return "\n".join(parts)


def extract_run_ref(merged: str) -> RunRef | None:
    """First `github.com/<owner>/<repo>/actions/runs/<id>` in the text."""
    m = _RUN_RE.search(merged)
    if m is None:
        return None
    return RunRef(repo=m.group(1), run_id=m.group(2))


def extract_pr_number(merged: str) -> int | None:
    m = _PR_RE.search(merged)
    if m is None:
        return None
    for g in m.groups():
        if g:
            return int(g)
    return None


def extract_head_sha(merged: str) -> str | None:
    m = _HEAD_SHA_RE.search(merged)
    return m.group(1) if m else None


def extract_failed_jobs(merged: str) -> tuple[str, ...]:
    jobs = [m.group(1).strip() for m in _FAILED_JOB_RE.finditer(merged)]
    return tuple(j for j in jobs if j)


def extract_consecutive_fail_count(merged: str) -> int | None:
    m = _CONSEC_RE.search(merged)
    return int(m.group(1)) if m else None


def extract_loki_window(merged: str) -> LokiWindow | None:
    """Best-effort host + time window from a `[host]` tag or a Grafana/Loki link.

    Feeds the device-level dual-evidence path. Returns None when no host is
    discoverable (handler then degrades to confidence=low rather than crash).
    """
    decoded = unquote(merged)
    host: str | None = None
    mb = _HOST_BRACKET_RE.search(merged)
    if mb is not None:
        host = mb.group(1)
    else:
        ml = _HOST_LOKI_RE.search(decoded)
        if ml is not None:
            host = ml.group(1)
    if host is None:
        return None
    start = _RANGE_FROM_RE.search(decoded)
    end = _RANGE_TO_RE.search(decoded)
    return LokiWindow(
        host=host,
        start=start.group(1) if start else None,
        end=end.group(1) if end else None,
    )


def extract_dut_hosts(text: str) -> tuple[str, ...]:
    """Candidate DUT (device-under-test) hostnames found in `text` (a job log),
    most-frequent first. The most-mentioned host is the test target — a premerge
    job's log mentions the DUT (ssw-host-04) far more than incidental hosts."""
    hits = [m.group(0).lower() for m in _DUT_HOST_RE.finditer(text)]
    return tuple(host for host, _count in Counter(hits).most_common())


# Fenced ``` code block contents (P3 log-only triage). Slack message text keeps
# the literal ``` markers, so this runs over the merged blob.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9]*\n?(.*?)```", re.DOTALL)
# CI/test/device-failure signatures — what makes a pasted log worth triaging.
_ERR_SIG_RE = re.compile(
    r"(?i)(\bERROR\b|\bFAIL(?:ED|URE)?\b|Traceback|\bpanic\b|\babort\b|"
    r"exit code\s*[1-9]|\btimed?\s*out\b|\btimeout\b|Unable to|No such file|"
    r"not ready|RETURN_STATUS|\brc\s*[-=]?\s*-?\d|0x[0-9a-fA-F]{6,})"
)
_MIN_LOG_BLOCK_LINES = 3


def extract_log_block(merged: str) -> str:
    """Concatenated contents of fenced ``` code blocks in the message (P3).
    Humans paste failing logs this way ("runfile install fail" + a ``` block);
    this is the evidence when there is no run link."""
    blocks = [m.group(1).strip() for m in _FENCE_RE.finditer(merged)]
    return "\n\n".join(b for b in blocks if b)


def has_error_signature(text: str) -> bool:
    """True when `text` looks like a CI/test/device failure (≥3 lines + an error
    signature) — the bar for triaging a pasted log with no run link."""
    if text.count("\n") + 1 < _MIN_LOG_BLOCK_LINES:
        return False
    return _ERR_SIG_RE.search(text) is not None


def is_ci_failure_candidate(
    msg: dict[str, Any],
    *,
    known_bot_ids: frozenset[str],
) -> bool:
    """A message is a CI-failure candidate if it is authored by a known alert bot,
    contains a `github.com/.../actions/runs/<id>` link, OR (P3) carries a fenced
    code block that looks like a failure log. Human chatter / Jira unfurls without
    any of these are ignored."""
    author = msg.get("user")
    if isinstance(author, str) and author in known_bot_ids:
        return True
    bot_id = msg.get("bot_id")
    if isinstance(bot_id, str) and bot_id in known_bot_ids:
        return True
    merged = merge_message_text(msg)
    if _RUN_RE.search(merged) is not None:
        return True
    return has_error_signature(extract_log_block(merged))


def parse_alert(msg: dict[str, Any], *, channel_id: str) -> ParsedAlert:
    """Build a `ParsedAlert` from a raw Slack message dict (or a re-parse of the
    stored `raw_blob`). `message_ts` is taken from `msg["ts"]`."""
    merged = merge_message_text(msg)
    author = msg.get("user")
    ts = msg.get("ts")
    return ParsedAlert(
        channel_id=channel_id,
        message_ts=str(ts) if ts is not None else "",
        author_id=author if isinstance(author, str) else None,
        merged_text=merged,
        run_ref=extract_run_ref(merged),
        pr_number=extract_pr_number(merged),
        head_sha=extract_head_sha(merged),
        failed_jobs=extract_failed_jobs(merged),
        consecutive_fail_count=extract_consecutive_fail_count(merged),
        loki_window=extract_loki_window(merged),
    )


__all__ = [
    "extract_consecutive_fail_count",
    "extract_dut_hosts",
    "extract_failed_jobs",
    "extract_head_sha",
    "extract_log_block",
    "extract_loki_window",
    "extract_pr_number",
    "extract_run_ref",
    "has_error_signature",
    "is_ci_failure_candidate",
    "merge_message_text",
    "parse_alert",
]
