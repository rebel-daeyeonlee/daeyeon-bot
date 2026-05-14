"""Jira wiki-markup helpers — 4-section writeup layout."""

from __future__ import annotations

from datetime import UTC, datetime

from daeyeon_bot.core.jira_triage.types import (
    EpicMeta,
    EvidenceItem,
    LokiSlice,
    RunMeta,
    RunSnapshot,
    SshArtifact,
    SuspectedDuplicate,
    TicketRef,
    TimeWindow,
    TitleParse,
    TriageDraft,
)
from daeyeon_bot.infra.jira_markup import (
    LogAttachments,
    bold,
    build_comment,
    build_log_attachments,
    bullet,
    code,
    code_block,
    duplicate_bullet,
    evidence_bullet,
    h3,
    noformat,
    quote,
    supersede_header_text,
)

# ── Primitive helpers ────────────────────────────────────────────────────────


def test_h3_emits_prefix() -> None:
    assert h3("Summary") == "h3. Summary"


def test_bullet_emits_prefix() -> None:
    assert bullet("a thing") == "* a thing"


def test_code_wraps_double_brace() -> None:
    assert code("FW HALT") == "{{FW HALT}}"


def test_code_escapes_closing_brace() -> None:
    """`}}` inside the body would close the span early; we space-break it."""
    assert code("see }} that") == "{{see } } that}}"


def test_noformat_wraps_with_newlines() -> None:
    out = noformat("line1\nline2")
    assert out.startswith("{noformat}\n")
    assert out.endswith("\n{noformat}")
    assert "line1\nline2" in out


def test_quote_inline_wrap() -> None:
    assert quote("hi") == "{quote}hi{quote}"


def test_bold_wraps_with_stars() -> None:
    assert bold("x") == "*x*"


def test_code_block_emits_title_and_close() -> None:
    out = code_block(title="loki.kernel — 3 lines", body="payload")
    assert out.startswith("{code:title=loki.kernel — 3 lines}\n")
    assert "payload" in out
    assert out.endswith("\n{code}")


def test_code_block_escapes_pipe_in_title() -> None:
    """`|` inside `{code:title=...}` terminates the title attribute — escape it."""
    out = code_block(title="ssh.dmesg | tail -100", body="x")
    assert "|" not in out.split("\n", 1)[0].replace("{code:title=", "")


# ── Fixtures ────────────────────────────────────────────────────────────────


def _draft(
    *,
    symptom: str = "rblnWaitJob TIMEDOUT 후 KMD TDR; root는 FW.",
    domain: str = "CpFw",
    layer_rationale: str = "err_code=0x10007이 0x1xxxx 범위로 CpFw page fault.",
    next_data: tuple[str, ...] = ("FW abort dump 캡처", "rblntrace로 재현"),
    duplicates: tuple[SuspectedDuplicate, ...] = (),
    needs_human: bool = False,
    evidence: tuple[EvidenceItem, ...] = (
        EvidenceItem(
            source="loki.kernel",
            quote="rbln_drv: TDR detected",
            citation="2026-05-13T06:55:12Z",
        ),
    ),
    severity: str = "sev2",
) -> TriageDraft:
    return TriageDraft(
        symptom=symptom,
        evidence=evidence,
        domain=domain,  # type: ignore[arg-type]
        layer_rationale=layer_rationale,
        next_data=next_data,
        severity=severity,  # type: ignore[arg-type]
        suspected_duplicates=duplicates,
        needs_human=needs_human,
    )


def _snapshot(
    *,
    error_log: str = "",
    test_code: str | None = None,
    loki_slices: tuple[LokiSlice, ...] = (),
    ssh_artifacts: tuple[SshArtifact, ...] = (),
    loki_error: str | None = None,
    ssh_error: str | None = None,
    tc_name: str = "TC-0033-Dram_test_with_exception",
) -> RunSnapshot:
    return RunSnapshot(
        meta=RunMeta(
            ticket=TicketRef(project="SSWCI", issue_key="SSWCI-100", created_iso="2026-05-13"),
            title=TitleParse(hostname="ssw-giga-02", tc_name=tc_name),
            epic=EpicMeta(epic_key="SSWCI-99", branch="dev", commit="a" * 7),
            window=TimeWindow(
                start_ts=datetime(2026, 5, 13, 6, 55, tzinfo=UTC),
                end_ts=datetime(2026, 5, 13, 7, 0, tzinfo=UTC),
                fallback=False,
            ),
            ssh=None,
            host_ip=None,
        ),
        error_log_excerpt=error_log,
        test_code=test_code,
        product_code=(),
        loki_slices=loki_slices,
        ssh_artifacts=ssh_artifacts,
        loki_error=loki_error,
        ssh_error=ssh_error,
    )


# ── build_comment shape ─────────────────────────────────────────────────────


def test_build_comment_has_all_four_sections() -> None:
    out = build_comment(_draft())
    assert "h3. Summary" in out
    assert "h3. Evidences" in out
    assert "h3. Analysis" in out
    assert "h3. Action Items" in out


def test_build_comment_starts_with_status_badge() -> None:
    out = build_comment(_draft(severity="sev1", domain="Driver", needs_human=True))
    first = out.split("\n", 1)[0]
    # 🔴 sev1 · *Driver* · needs_human
    assert "🔴" in first
    assert "sev1" in first
    assert "Driver" in first
    assert "needs_human" in first


def test_build_comment_ends_with_signoff() -> None:
    out = build_comment(_draft())
    assert "_— daeyeon-bot 🐥_" in out
    assert out.endswith("\n")


def test_build_comment_with_supersede_header() -> None:
    out = build_comment(_draft(), supersede_header=supersede_header_text("14:30:11 UTC"))
    assert out.startswith(
        "{quote}Updated triage (supersedes earlier bot comment posted at 14:30:11 UTC).{quote}"
    )


def test_build_comment_no_panels() -> None:
    """4-section writeup intentionally avoids `{panel}` blocks."""
    out = build_comment(_draft())
    assert "{panel:" not in out


def test_build_comment_korean_prose_passes_through() -> None:
    out = build_comment(_draft(symptom="rblnWaitJob TIMEDOUT 후 다음 잡 제출 실패."))
    assert "rblnWaitJob TIMEDOUT 후" in out


def test_build_comment_with_duplicates_renders_section() -> None:
    dups = (SuspectedDuplicate(key="SSWCI-1234", basis="same TC + same err_code"),)
    out = build_comment(_draft(duplicates=dups))
    assert "Suspected duplicates" in out
    assert "*SSWCI-1234*" in out


def test_build_comment_routes_evidence_by_source() -> None:
    """log/ssh sources go into Evidences; test_code/product_code go into Analysis."""
    evidence = (
        EvidenceItem(source="loki.kernel", quote="LOG_LINE", citation="2026-05-13T06:55:12Z"),
        EvidenceItem(source="test_code", quote="TC-x", citation="t.robot:42"),
    )
    out = build_comment(_draft(evidence=evidence))
    ev_idx = out.index("h3. Evidences")
    an_idx = out.index("h3. Analysis")
    actions_idx = out.index("h3. Action Items")
    evidences_section = out[ev_idx:an_idx]
    analysis_section = out[an_idx:actions_idx]
    assert "loki.kernel" in evidences_section
    assert "test_code" in analysis_section
    # Cross-check the wrong way around isn't true:
    assert "test_code" not in evidences_section


def test_build_comment_action_items_renders_bullets() -> None:
    out = build_comment(_draft(next_data=("a", "b", "c")))
    assert "* a" in out
    assert "* b" in out
    assert "* c" in out


# ── build_log_attachments — windowing + expand block emission ───────────────


def test_attachments_windows_around_cited_quote_in_loki_kernel() -> None:
    """Cited line gets ±5-line window, lines numbered, emitted as `{code}`."""
    lines = tuple(f"kernel line {i}" for i in range(1, 21))
    slc = LokiSlice(stream="kernel", lines=lines, truncated=False)
    snap = _snapshot(loki_slices=(slc,))
    triage = _draft(
        evidence=(
            EvidenceItem(
                source="loki.kernel",
                quote="kernel line 10",
                citation="2026-05-13T06:55:12Z",
            ),
        )
    )
    atts = build_log_attachments(snap, triage)
    block = atts.expand_blocks["loki.kernel"]
    assert block.startswith("{code:title=loki.kernel —")
    # The window is lines 10 ± 5 = lines 5..15 (0-based 4..14 → 1-based 5..15).
    assert "kernel line 10" in block
    assert "kernel line 5" in block
    assert "kernel line 15" in block
    # Lines outside window NOT included.
    assert "kernel line 1\n" not in block.replace("kernel line 10", "")  # crude check
    assert "kernel line 20" not in block


def test_attachments_merge_overlapping_windows() -> None:
    """Two cites within ±5 lines collapse to one merged expand."""
    lines = tuple(f"line {i}" for i in range(1, 21))
    slc = LokiSlice(stream="kernel", lines=lines, truncated=False)
    snap = _snapshot(loki_slices=(slc,))
    triage = _draft(
        evidence=(
            EvidenceItem(source="loki.kernel", quote="line 8", citation="t1"),
            EvidenceItem(source="loki.kernel", quote="line 11", citation="t2"),
        )
    )
    atts = build_log_attachments(snap, triage)
    block = atts.expand_blocks["loki.kernel"]
    # Merged window covers lines 3..16. The `...` separator should NOT appear.
    assert "\n...\n" not in block


def test_attachments_distant_quotes_render_two_windows_with_separator() -> None:
    lines = tuple(f"line {i}" for i in range(1, 41))
    slc = LokiSlice(stream="kernel", lines=lines, truncated=False)
    snap = _snapshot(loki_slices=(slc,))
    triage = _draft(
        evidence=(
            EvidenceItem(source="loki.kernel", quote="line 5", citation="t1"),
            EvidenceItem(source="loki.kernel", quote="line 30", citation="t2"),
        )
    )
    atts = build_log_attachments(snap, triage)
    block = atts.expand_blocks["loki.kernel"]
    assert "\n...\n" in block


def test_attachments_skip_source_without_cite() -> None:
    """A loki stream with data but no cite: stream_status 1-liner, no expand."""
    slc = LokiSlice(stream="syslog", lines=("noise", "more noise"), truncated=False)
    snap = _snapshot(loki_slices=(slc,))
    atts = build_log_attachments(snap, _draft())
    assert "loki.syslog" not in atts.expand_blocks
    assert atts.stream_status["loki.syslog"].startswith("*loki.syslog* — 2 lines (not cited)")


def test_attachments_ssh_error_surfaces_as_diagnostic() -> None:
    snap = _snapshot(ssh_error="auth_failed")
    atts = build_log_attachments(snap, _draft())
    assert atts.stream_status["ssh"] == "*ssh* — auth_failed"
    assert "ssh" not in atts.expand_blocks


def test_attachments_loki_error_per_stream_surfaces() -> None:
    snap = _snapshot(loki_error="fwlog:dns_failed; smclog:timeout")
    atts = build_log_attachments(snap, _draft())
    assert "loki.fwlog" in atts.stream_status
    assert "dns_failed" in atts.stream_status["loki.fwlog"]
    assert "loki.smclog" in atts.stream_status
    assert "timeout" in atts.stream_status["loki.smclog"]


def test_attachments_ticket_error_log_windowed() -> None:
    error_log = "\n".join(f"err line {i}" for i in range(1, 31))
    snap = _snapshot(error_log=error_log)
    triage = _draft(
        evidence=(EvidenceItem(source="ticket.error_log", quote="err line 15", citation=":15"),)
    )
    atts = build_log_attachments(snap, triage)
    block = atts.expand_blocks["ticket.error_log"]
    assert "err line 10" in block
    assert "err line 15" in block
    assert "err line 20" in block
    assert "err line 1\n" not in block
    assert "err line 30" not in block


def test_attachments_test_code_renders_tc_block() -> None:
    test_code = (
        "*** Test Cases ***\n"
        "Other-TC\n"
        "    [Documentation]    not our TC\n"
        "    Log    ok\n"
        "\n"
        "TC-0033-Dram_test_with_exception\n"
        "    [Documentation]    real TC\n"
        "    Run Keyword    foo\n"
        "    Run Keyword    bar\n"
        "\n"
        "Yet-Another-TC\n"
        "    Log    other\n"
    )
    snap = _snapshot(test_code=test_code)
    triage = _draft(
        evidence=(EvidenceItem(source="test_code", quote="Run Keyword    foo", citation=":7"),)
    )
    atts = build_log_attachments(snap, triage)
    block = atts.expand_blocks["test_code"]
    assert "TC-0033-Dram_test_with_exception" in block
    assert "Run Keyword    foo" in block
    # Should stop before the next TC.
    assert "Yet-Another-TC" not in block


# ── build_comment integration with attachments ──────────────────────────────


def test_build_comment_embeds_expand_blocks_for_cited_sources() -> None:
    slc = LokiSlice(stream="kernel", lines=("aaa", "TDR detected", "bbb"), truncated=False)
    snap = _snapshot(loki_slices=(slc,))
    triage = _draft(
        evidence=(
            EvidenceItem(source="loki.kernel", quote="TDR detected", citation="2026-05-13T07Z"),
        )
    )
    atts = build_log_attachments(snap, triage)
    out = build_comment(triage, attachments=atts)
    assert "{code:title=loki.kernel —" in out
    assert "TDR detected" in out


def test_build_comment_truncates_to_body_cap() -> None:
    """A pathologically huge attachment should be dropped to fit comment limit."""
    huge_lines = tuple(f"huge line {i}: {'x' * 200}" for i in range(1, 500))
    slc = LokiSlice(stream="kernel", lines=huge_lines, truncated=False)
    snap = _snapshot(loki_slices=(slc,))
    # Pad evidence so windows cover enough of the huge slice.
    quotes = [f"huge line {i}:" for i in range(1, 500, 12)]
    evidence = tuple(
        EvidenceItem(source="loki.kernel", quote=q, citation=f"t{i}") for i, q in enumerate(quotes)
    )
    triage = _draft(evidence=evidence)
    atts = build_log_attachments(snap, triage)
    out = build_comment(triage, attachments=atts)
    assert len(out.encode("utf-8")) <= 30_000
    assert "trimmed to fit" in out or "truncated" in out


# ── evidence_bullet / duplicate_bullet helpers ───────────────────────────────


def test_evidence_bullet_short_quote_uses_inline_code() -> None:
    item = EvidenceItem(source="ssh.dmesg", quote="atom_halt status: 6", citation="ssh.dmesg:1247")
    out = evidence_bullet(item)
    assert out == "* *ssh.dmesg* @ ssh.dmesg:1247 — {{atom_halt status: 6}}"


def test_evidence_bullet_long_quote_uses_noformat() -> None:
    item = EvidenceItem(source="ssh.dmesg", quote="x" * 250, citation="ssh.dmesg:99")
    assert "{noformat}" in evidence_bullet(item)


def test_evidence_bullet_multiline_quote_uses_noformat() -> None:
    item = EvidenceItem(source="ssh.dmesg", quote="line1\nline2", citation="ssh.dmesg:42")
    out = evidence_bullet(item)
    assert "{noformat}" in out
    assert "line1" in out


def test_duplicate_bullet_renders_bold_key() -> None:
    dup = SuspectedDuplicate(key="SSWCI-99", basis="same TC")
    assert duplicate_bullet(dup) == "* *SSWCI-99* — same TC"


def test_log_attachments_default_factory_is_empty() -> None:
    atts = LogAttachments()
    assert atts.expand_blocks == {}
    assert atts.stream_status == {}
