"""Pure helpers to turn a `gh run view --log-failed` dump into Claude input.

Order is load-bearing: **strip ANSI first, then redact, then error-anchored
truncate**. A measured dump is ~438 KB / 3000+ lines whose head is GITHUB_TOKEN
boilerplate and whose real cause is buried mid-file, so we never head/tail — we
collect windows around error anchors. Redaction is applied by the caller via
`infra/logging.py` (reused); this module does the ANSI strip + truncation.

See specs/003-ci-monitor-bot/plan.md §Pipeline (EVIDENCE).
"""

from __future__ import annotations

import re

# CSI / SGR ANSI escape sequences (colors, cursor moves) that GitHub log lines carry.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# GitHub log line prefix: "<job path>\t<STEP>\t<RFC3339 ts> <message>".
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")

# Anchors that mark the real failure signal (case-insensitive substring match).
_ANCHORS = (
    "##[error]",
    "process completed with exit code",
    "error -",
    "fail",
    "test failed",
    "traceback",
    "fatal",
    "no such file",
    "timeout",
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (so a color-split secret is still caught by
    redaction, and the text is readable)."""
    return _ANSI_RE.sub("", text)


def _clean_line(raw: str) -> str:
    """Drop the GitHub `<job>\\t<step>\\t` columns and the leading RFC3339 ts,
    keeping the human-readable message (+ a short job tag for context)."""
    parts = raw.split("\t")
    if len(parts) >= 3:
        job = parts[0].strip()
        msg = _TS_RE.sub("", parts[-1]).rstrip()
        return f"{job} | {msg}" if job else msg
    return _TS_RE.sub("", raw).rstrip()


def error_anchored_windows(
    log_text: str,
    *,
    context: int = 8,
    max_chars: int = 12_000,
) -> str:
    """Return the error-anchored slices of an ANSI-stripped log.

    For each line containing an anchor, include `context` lines before/after.
    Overlapping windows merge. Output is capped at `max_chars` (newest anchors
    win — the failing tail is the most relevant). Returns the whole (cleaned)
    log when it is already under the cap and no anchor matched (small logs)."""
    lines = strip_ansi(log_text).splitlines()
    if not lines:
        return ""

    hit_idx = [i for i, ln in enumerate(lines) if any(a in ln.lower() for a in _ANCHORS)]
    if not hit_idx:
        cleaned = "\n".join(_clean_line(ln) for ln in lines)
        return cleaned[-max_chars:]

    # Build merged [start, end) windows around each anchor.
    windows: list[tuple[int, int]] = []
    for i in hit_idx:
        start = max(0, i - context)
        end = min(len(lines), i + context + 1)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))

    chunks: list[str] = []
    for start, end in windows:
        body = "\n".join(_clean_line(lines[j]) for j in range(start, end))
        chunks.append(f"--- log lines {start + 1}-{end} ---\n{body}")
    joined = "\n\n".join(chunks)
    if len(joined) <= max_chars:
        return joined
    # Keep the tail (latest anchors = the actual failure) within budget.
    return "…(truncated)…\n" + joined[-max_chars:]


__all__ = ["error_anchored_windows", "strip_ansi"]
