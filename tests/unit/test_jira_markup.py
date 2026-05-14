"""Jira wiki-markup helpers — T020 tests."""

from __future__ import annotations

from daeyeon_bot.core.jira_triage.types import (
    EvidenceItem,
    SuspectedDuplicate,
    TriageDraft,
)
from daeyeon_bot.infra.jira_markup import (
    bold,
    build_comment,
    bullet,
    code,
    duplicate_bullet,
    evidence_bullet,
    h3,
    noformat,
    quote,
    supersede_header_text,
)


def test_h3_emits_prefix() -> None:
    assert h3("Symptom") == "h3. Symptom"


def test_bullet_emits_prefix() -> None:
    assert bullet("a thing") == "* a thing"


def test_code_wraps_double_brace() -> None:
    assert code("FW HALT") == "{{FW HALT}}"


def test_code_escapes_closing_brace() -> None:
    """`}}` inside the body would close the span early; we space-break it."""
    out = code("see }} that")
    assert "}}" not in out.replace("{{", "").replace(out[-2:], "")  # last 2 chars are closing


def test_noformat_wraps_with_newlines() -> None:
    out = noformat("line1\nline2")
    assert out.startswith("{noformat}\n")
    assert out.endswith("\n{noformat}")


def test_quote_inline_wrap() -> None:
    assert quote("hi") == "{quote}hi{quote}"


def test_bold_wraps_with_stars() -> None:
    assert bold("x") == "*x*"


# ── build_comment integration ────────────────────────────────────────────────


def _draft(
    *,
    summary_md: str = "h3. Symptom\nfoo\n\nh3. Evidence cited\n* a @ b — {{c}}",
    domain: str = "CpFw",
    duplicates: tuple[SuspectedDuplicate, ...] = (),
    needs_human: bool = False,
    evidence: tuple[EvidenceItem, ...] = (
        EvidenceItem(
            source="loki.kernel", quote="rbln_drv: TDR detected", citation="2026-05-13T06:55:12Z"
        ),
    ),
) -> TriageDraft:
    return TriageDraft(
        summary_md=summary_md,
        domain=domain,  # type: ignore[arg-type]
        severity="sev2",
        suspected_duplicates=duplicates,
        needs_human=needs_human,
        evidence=evidence,
    )


def test_build_comment_no_supersede_no_duplicates() -> None:
    out = build_comment(_draft())
    assert "h3. Symptom" in out
    assert "{quote}" not in out
    assert "Suspected duplicates" not in out


def test_build_comment_with_supersede_header() -> None:
    out = build_comment(_draft(), supersede_header=supersede_header_text("14:30:11 UTC"))
    assert out.startswith(
        "{quote}Updated triage (supersedes earlier bot comment posted at 14:30:11 UTC).{quote}"
    )
    # body still follows
    assert "h3. Symptom" in out


def test_build_comment_with_duplicates() -> None:
    dups = (
        SuspectedDuplicate(key="SSWCI-1234", basis="same TC + same err_code"),
        SuspectedDuplicate(key="SSWCI-5678", basis="adjacent host history"),
    )
    out = build_comment(_draft(duplicates=dups))
    assert "Suspected duplicates (best-effort, NOT verified)" in out
    assert "*SSWCI-1234*" in out
    assert "*SSWCI-5678*" in out


def test_build_comment_needs_human_quote_appended() -> None:
    out = build_comment(_draft(needs_human=True))
    assert "{quote}needs_human=true" in out


def test_build_comment_ends_with_newline() -> None:
    out = build_comment(_draft())
    assert out.endswith("\n")


def test_build_comment_korean_passes_through() -> None:
    """Korean prose in `summary_md` is preserved verbatim (SC-012)."""
    summary = "h3. Symptom\nrblnWaitJob TIMEDOUT 후 다음 잡 제출 실패."
    out = build_comment(_draft(summary_md=summary))
    assert "rblnWaitJob TIMEDOUT 후" in out


# ── evidence_bullet / duplicate_bullet helpers ───────────────────────────────


def test_evidence_bullet_short_quote_uses_inline_code() -> None:
    item = EvidenceItem(source="ssh.dmesg", quote="atom_halt status: 6", citation="ssh.dmesg:1247")
    out = evidence_bullet(item)
    assert out.startswith("* ssh.dmesg @ ssh.dmesg:1247 — {{atom_halt status: 6}}")


def test_evidence_bullet_long_quote_uses_noformat() -> None:
    long_quote = "x" * 250
    item = EvidenceItem(source="ssh.dmesg", quote=long_quote, citation="ssh.dmesg:99")
    out = evidence_bullet(item)
    assert "{noformat}" in out


def test_evidence_bullet_multiline_quote_uses_noformat() -> None:
    item = EvidenceItem(source="ssh.dmesg", quote="line1\nline2", citation="ssh.dmesg:42")
    out = evidence_bullet(item)
    assert "{noformat}" in out
    assert "line1" in out


def test_duplicate_bullet_renders_bold_key() -> None:
    dup = SuspectedDuplicate(key="SSWCI-99", basis="same TC")
    assert duplicate_bullet(dup) == "* *SSWCI-99* — same TC"
