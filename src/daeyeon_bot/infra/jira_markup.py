"""Jira wiki-markup helpers for the triage comment body.

The bot posts comments via REST v2 (`POST /rest/api/2/issue/{key}/comment`)
which accepts a plain-string `body` in Jira wiki markup. This module
assembles a 4-section "writeup" style comment from a `TriageDraft` plus
a set of `LogAttachments` (windowed excerpts from the Run Snapshot).

Layout (top-to-bottom):

  [supersede header — only when force-superseding]

  *<sev emoji>* <sev> · *<Domain>* · <auto|needs_human>

  h3. Summary
  <symptom>

  h3. Evidences
  *<source>* @ <citation> — {{<quote>}}
  ...
  {code:title=...}<windowed excerpt>{code}

  h3. Analysis
  <layer_rationale paragraph>
  *<source>* @ <citation> — {{<quote>}}
  {code:title=...}<TC block / code excerpt>{code}

  h3. Action Items
  * <imperative>
  ...

  [Suspected duplicates — when non-empty]

  _— daeyeon-bot 🐥_

Attachment policy — we never dump full slices; only **±5 lines around
each cited evidence quote**, overlapping windows merged. Empty / errored
streams render as a one-line diagnostic, no {code} block. Total body
is capped at `_BODY_CAP_BYTES`; tail attachments are truncated to fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from daeyeon_bot.core.jira_triage.types import (
    EvidenceItem,
    RunSnapshot,
    SuspectedDuplicate,
    TriageDraft,
)

# Per-severity badge.
_SEVERITY_EMOJI: dict[str, str] = {
    "sev1": "🔴",
    "sev2": "🟠",
    "sev3": "🟡",
    "unknown": "⚪",
}

_BODY_CAP_BYTES = 30_000  # Jira hard limit is 32 KB; leave a safety margin.
_WINDOW_PADDING = 5  # ±N lines around each cited quote inside an excerpt
_TC_BLOCK_MAX_LINES = 60
_EXCERPT_LINE_PREFIX_W = 5  # width of "<line> | " line-number prefix


# ── Primitive helpers ────────────────────────────────────────────────────────


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
    """`{noformat}…{noformat}` block. Trailing/leading newlines normalized."""
    return "{noformat}\n" + text.rstrip() + "\n{noformat}"


def quote(text: str) -> str:
    """`{quote}…{quote}` inline-wrapped block."""
    return "{quote}" + text + "{quote}"


def bold(text: str) -> str:
    """`*bold*` inline."""
    return f"*{text}*"


def code_block(*, title: str, body: str) -> str:
    """Render a Jira `{code:title=...}` block.

    Used for log excerpts attached to the comment. Cloud's v2 wiki parser
    doesn't process the `{expand}` macro (it survives as literal text),
    so we use `{code}` which IS supported and gives a clean titled box
    with monospace body and a visible delimiter. No collapse — content
    is always shown.

    The `|` inside the macro head terminates the title attribute, so any
    pipe in `title` is escaped.
    """
    safe_title = title.replace("|", "/")
    return f"{{code:title={safe_title}}}\n{body.rstrip()}\n{{code}}"


# ── Attachment building ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _LineWindow:
    start: int  # 0-based inclusive
    end: int  # 0-based inclusive


@dataclass(frozen=True, slots=True)
class LogAttachments:
    """Pre-rendered `{expand}` blocks the comment will attach, keyed by source.

    Built from RunSnapshot + TriageDraft. Each value is a complete
    `{expand:title=...}{noformat}<excerpt>{noformat}{expand}` string,
    or the empty string when there's nothing to attach for that source.
    A `stream_status[source]` 1-liner (e.g. `"loki.fwlog — 0 lines"`)
    is rendered separately under the Evidences section for sources
    that don't carry a cited quote but are diagnostically interesting.
    """

    expand_blocks: dict[str, str] = field(default_factory=dict)
    stream_status: dict[str, str] = field(default_factory=dict)


def build_log_attachments(  # noqa: PLR0912 — fan-out per evidence source type
    snapshot: RunSnapshot, triage: TriageDraft
) -> LogAttachments:
    """Build {expand} blocks for sources with cited evidence; diagnostic
    one-liners for streams that returned empty / errored.

    Per cited quote: a ±N-line window around the matching line. Overlapping
    windows within a single source are merged.
    """
    cites_by_source: dict[str, list[str]] = {}
    for item in triage.evidence:
        cites_by_source.setdefault(item.source, []).append(item.quote)

    expand_blocks: dict[str, str] = {}
    stream_status: dict[str, str] = {}

    # ── ticket.error_log ─────────────────────────────────────────────────────
    if "ticket.error_log" in cites_by_source and snapshot.error_log_excerpt:
        block = _render_windowed_block(
            source="ticket.error_log",
            text=snapshot.error_log_excerpt,
            quotes=cites_by_source["ticket.error_log"],
        )
        if block:
            expand_blocks["ticket.error_log"] = block

    # ── Loki streams (fwlog / smclog / kernel / syslog) ──────────────────────
    loki_present = {slc.stream for slc in snapshot.loki_slices}
    for slc in snapshot.loki_slices:
        source = f"loki.{slc.stream}"
        if source in cites_by_source:
            block = _render_windowed_block(
                source=source,
                text="\n".join(slc.lines),
                quotes=cites_by_source[source],
            )
            if block:
                expand_blocks[source] = block
        else:
            # No cite but the stream returned data — flag it as 1-liner.
            stream_status[source] = f"*{source}* — {len(slc.lines)} lines (not cited)"

    # Streams that failed entirely (no slice, no cite) — render diagnostic.
    if snapshot.loki_error:
        for label_err_raw in snapshot.loki_error.split(";"):
            label_err = label_err_raw.strip()
            if not label_err or ":" not in label_err:
                continue
            stream_name, err = label_err.split(":", 1)
            stream_name = stream_name.strip()
            err = err.strip()
            if stream_name in {"fwlog", "smclog", "kernel", "syslog"}:
                if stream_name not in loki_present:
                    stream_status[f"loki.{stream_name}"] = f"*loki.{stream_name}* — {err}"

    # ── SSH artifacts ────────────────────────────────────────────────────────
    for art in snapshot.ssh_artifacts:
        source = _ssh_source_label(art.filename)
        if source in cites_by_source and art.contents:
            block = _render_windowed_block(
                source=source,
                text=art.contents,
                quotes=cites_by_source[source],
            )
            if block:
                expand_blocks[source] = block
        else:
            stream_status[source] = f"*{source}* — {art.size_bytes} bytes (not cited)"

    if snapshot.ssh_error:
        stream_status["ssh"] = f"*ssh* — {snapshot.ssh_error}"

    # ── test_code (TC block, not windowed) ───────────────────────────────────
    if (
        "test_code" in cites_by_source
        and snapshot.test_code is not None
        and snapshot.meta.title.tc_name
    ):
        block = _render_tc_block(snapshot.test_code, snapshot.meta.title.tc_name)
        if block:
            expand_blocks["test_code"] = block

    # ── product_code (windowed) ──────────────────────────────────────────────
    if "product_code" in cites_by_source and snapshot.product_code:
        joined = "\n".join(p.excerpt for p in snapshot.product_code)
        block = _render_windowed_block(
            source="product_code",
            text=joined,
            quotes=cites_by_source["product_code"],
        )
        if block:
            expand_blocks["product_code"] = block

    return LogAttachments(expand_blocks=expand_blocks, stream_status=stream_status)


def _ssh_source_label(filename: str) -> str:
    """Map SSH filename → canonical evidence source label."""
    if filename == "output.xml":
        return "ssh.output_xml"
    if filename == "dmesg.log":
        return "ssh.dmesg"
    if filename == "console.log":
        return "ssh.console"
    return f"ssh.{filename}"


def _render_windowed_block(*, source: str, text: str, quotes: list[str]) -> str:
    """Find each quote's line, build merged ±N-line windows, render as expand."""
    lines = text.split("\n")
    windows = _windows_for_quotes(lines, quotes, padding=_WINDOW_PADDING)
    if not windows:
        return ""
    rendered = _render_excerpt(lines, windows)
    total = sum(w.end - w.start + 1 for w in windows)
    title = f"{source} — {total} lines around cited evidence"
    return code_block(title=title, body=rendered)


def _render_tc_block(test_code: str, tc_name: str) -> str:
    """Extract the named TC block (column-0 header → next column-0 ID, max N lines)."""
    lines = test_code.split("\n")
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(tc_name) and (len(line) == len(tc_name) or line[len(tc_name)].isspace()):
            start = i
            break
    if start is None:
        return ""
    end = min(start + _TC_BLOCK_MAX_LINES, len(lines))
    # Stop at next column-0 identifier or section heading.
    for j in range(start + 1, end):
        line = lines[j]
        if not line:
            continue
        if line[0].isalpha() and not line[0].islower():
            # Heuristic: another Test Case header (column-0, capitalized).
            end = j
            break
        if line.startswith("*** "):
            # Robot Framework section marker.
            end = j
            break
    block_lines = lines[start:end]
    rendered = _render_excerpt(
        block_lines, [_LineWindow(0, len(block_lines) - 1)], base_line=start + 1
    )
    title = f"test_code (TC block, lines {start + 1}-{end})"
    return code_block(title=title, body=rendered)


def _windows_for_quotes(lines: list[str], quotes: list[str], *, padding: int) -> list[_LineWindow]:
    """For each quote, find its line and build a ±padding window. Merge overlaps."""
    raw: list[_LineWindow] = []
    for q in quotes:
        idx = _find_quote_line(lines, q)
        if idx is None:
            continue
        raw.append(
            _LineWindow(
                start=max(0, idx - padding),
                end=min(len(lines) - 1, idx + padding),
            )
        )
    return _merge_windows(raw)


def _find_quote_line(lines: list[str], quote: str) -> int | None:
    """First line index containing `quote` as a substring.

    For multi-line quotes, returns the line where the first line of the
    quote appears. (`evidence` validation has already proven the quote
    exists in the joined text.)
    """
    head = quote.split("\n", 1)[0]
    for i, line in enumerate(lines):
        if head in line:
            return i
    return None


def _merge_windows(windows: list[_LineWindow]) -> list[_LineWindow]:
    """Sort + coalesce overlapping/adjacent windows."""
    if not windows:
        return []
    sorted_w = sorted(windows, key=lambda w: w.start)
    merged: list[_LineWindow] = [sorted_w[0]]
    for w in sorted_w[1:]:
        last = merged[-1]
        if w.start <= last.end + 1:
            merged[-1] = _LineWindow(start=last.start, end=max(last.end, w.end))
        else:
            merged.append(w)
    return merged


def _render_excerpt(lines: list[str], windows: list[_LineWindow], *, base_line: int = 1) -> str:
    """Emit `<line> | <content>` rows for each window, separated by `...`.

    `base_line` is the 1-based line number to print at index 0 (used so
    test_code excerpts show the real file line numbers, not 1).
    """
    parts: list[str] = []
    for i, w in enumerate(windows):
        if i > 0:
            parts.append("...")
        for j in range(w.start, w.end + 1):
            ln = base_line + j
            parts.append(f"{ln:>{_EXCERPT_LINE_PREFIX_W}} | {lines[j]}")
    return "\n".join(parts)


# ── Top-level assembly ──────────────────────────────────────────────────────


def build_comment(
    triage: TriageDraft,
    *,
    attachments: LogAttachments | None = None,
    supersede_header: str | None = None,
) -> str:
    """Assemble the 4-section comment body.

    Sections, in order:
      1. (optional) supersede header in `{quote}`
      2. status badge line — `*<emoji>* sev · *Domain* · auto|needs_human`
      3. h3. Summary  — `symptom`
      4. h3. Evidences — bullets per cited log/ticket evidence + `{expand}` blocks
      5. h3. Analysis — `layer_rationale` paragraph + code-evidence bullets + test_code/product_code `{expand}`
      6. h3. Action Items — `next_data` bullets
      7. (optional) Suspected duplicates
      8. Sign-off

    Total body is byte-capped; tail `{expand}` blocks are dropped if needed.
    """
    atts = attachments or LogAttachments()
    parts: list[str] = []

    if supersede_header:
        parts.append(quote(supersede_header))
        parts.append("")

    parts.append(_status_line(triage))
    parts.append("")

    parts.append(h3("Summary"))
    parts.append("")
    parts.append(triage.symptom.rstrip())
    parts.append("")

    parts.extend(_evidences_section(triage, atts))
    parts.extend(_analysis_section(triage, atts))
    parts.extend(_action_items_section(triage))

    if triage.suspected_duplicates:
        parts.extend(_duplicates_section(triage.suspected_duplicates))

    parts.append("_— daeyeon-bot 🐥_")

    body = "\n".join(parts) + "\n"
    return _truncate_to_cap(body)


def _status_line(triage: TriageDraft) -> str:
    flag = "needs_human" if triage.needs_human else "auto"
    emoji = _SEVERITY_EMOJI.get(triage.severity, "⚪")
    return f"{bold(emoji)} {triage.severity} · {bold(triage.domain)} · {flag}"


def _evidences_section(triage: TriageDraft, atts: LogAttachments) -> list[str]:
    log_sources = {"ticket.error_log", "loki.fwlog", "loki.smclog", "loki.kernel", "loki.syslog"}
    ssh_sources = {"ssh.output_xml", "ssh.dmesg", "ssh.console"}
    log_or_ssh = log_sources | ssh_sources

    cites = [e for e in triage.evidence if e.source in log_or_ssh]

    parts: list[str] = [h3("Evidences"), ""]
    if cites:
        for item in cites:
            parts.append(evidence_bullet(item))
    else:
        parts.append("_(no log/ssh evidence cited)_")
    parts.append("")

    # Diagnostic 1-liners — sources with no cite but worth flagging.
    diagnostic_keys = [k for k in atts.stream_status if k not in {e.source for e in cites}]
    for key in sorted(diagnostic_keys):
        parts.append(bullet(atts.stream_status[key]))
    if diagnostic_keys:
        parts.append("")

    # Expand blocks — one per cited source, sorted for stable output.
    expand_keys_in_section = sorted(k for k in atts.expand_blocks if k in log_or_ssh)
    for key in expand_keys_in_section:
        parts.append(atts.expand_blocks[key])
        parts.append("")

    return parts


def _analysis_section(triage: TriageDraft, atts: LogAttachments) -> list[str]:
    code_sources = {"test_code", "product_code"}
    code_cites = [e for e in triage.evidence if e.source in code_sources]

    parts: list[str] = [h3("Analysis"), ""]
    parts.append(f"Likely layer: {bold(triage.domain)}")
    parts.append("")
    parts.append(triage.layer_rationale.rstrip())
    parts.append("")

    if code_cites:
        for item in code_cites:
            parts.append(evidence_bullet(item))
        parts.append("")

    for key in sorted(k for k in atts.expand_blocks if k in code_sources):
        parts.append(atts.expand_blocks[key])
        parts.append("")

    return parts


def _action_items_section(triage: TriageDraft) -> list[str]:
    parts: list[str] = [h3("Action Items"), ""]
    if triage.next_data:
        for item in triage.next_data:
            parts.append(bullet(item))
    else:
        parts.append("_(no follow-up actions suggested)_")
    parts.append("")
    return parts


def _duplicates_section(dups: tuple[SuspectedDuplicate, ...]) -> list[str]:
    parts: list[str] = [h3("Suspected duplicates (best-effort, NOT verified)"), ""]
    for d in dups:
        parts.append(duplicate_bullet(d))
    parts.append("")
    return parts


def _truncate_to_cap(body: str) -> str:
    """Drop trailing `{code:title=...}` blocks (and their preceding blank) until under cap."""
    if len(body.encode("utf-8")) <= _BODY_CAP_BYTES:
        return body
    parts = body.split("\n")
    while len(("\n".join(parts) + "\n").encode("utf-8")) > _BODY_CAP_BYTES:
        close_idx: int | None = None
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].strip() == "{code}":
                close_idx = i
                break
        if close_idx is None:
            # No more {code} blocks — fall back to char truncation.
            text = "\n".join(parts)
            cap_chars = _BODY_CAP_BYTES - 256
            return text[:cap_chars] + "\n... [body truncated to fit comment limit]\n"
        open_idx = close_idx
        for j in range(close_idx - 1, -1, -1):
            if parts[j].startswith("{code:title="):
                open_idx = j
                break
        del parts[open_idx : close_idx + 1]
        if open_idx < len(parts) and parts[open_idx] == "":
            del parts[open_idx]
    truncated = "\n".join(parts)
    if not truncated.endswith("\n"):
        truncated += "\n"
    truncated += "_(some log excerpts trimmed to fit Jira comment limit)_\n"
    return truncated


def supersede_header_text(prior_posted_at_hhmmss_utc: str) -> str:
    """Standard supersede-header text used as the leading `{quote}` block."""
    return (
        f"Updated triage (supersedes earlier bot comment posted at {prior_posted_at_hhmmss_utc})."
    )


def evidence_bullet(item: EvidenceItem) -> str:
    """Format: `* *<source>* @ <citation> — {{<quote>}}` (short) or `{noformat}` (long)."""
    head = f"{bold(item.source)} @ {item.citation}"
    if len(item.quote) > 200 or "\n" in item.quote:
        return f"{bullet(head + ' —')}\n{noformat(item.quote)}"
    return bullet(f"{head} — {code(item.quote)}")


def duplicate_bullet(dup: SuspectedDuplicate) -> str:
    return bullet(f"{bold(dup.key)} — {dup.basis}")


__all__ = [
    "LogAttachments",
    "bold",
    "build_comment",
    "build_log_attachments",
    "bullet",
    "code",
    "code_block",
    "duplicate_bullet",
    "evidence_bullet",
    "h3",
    "noformat",
    "quote",
    "supersede_header_text",
]
