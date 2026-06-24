"""Cross-run comparison analysis (feature 003 P1)."""

from __future__ import annotations

from daeyeon_bot.handlers.ci_triage_cross_run import analyze_cross_run
from daeyeon_bot.infra.gh_cli import RunSummary


def _run(sha: str, conclusion: str) -> RunSummary:
    return RunSummary(
        id=sha,
        head_sha=sha,
        head_branch="b",
        status="completed",
        conclusion=conclusion,
        event="pull_request",
        created_at="2026-06-24T00:00:00Z",
    )


def test_systemic_when_other_prs_also_fail() -> None:
    runs = [_run("mine", "failure"), *(_run(f"o{i}", "failure") for i in range(3))]
    r = analyze_cross_run(head_sha="mine", runs=runs)
    assert r.verdict == "systemic"
    assert r.signal is True
    assert r.others_total == 3 and r.others_failed == 3
    assert "환경·인프라 유력" in (r.summary_ko() or "")


def test_isolated_when_only_this_pr_fails() -> None:
    runs = [_run("mine", "failure"), *(_run(f"o{i}", "success") for i in range(4))]
    r = analyze_cross_run(head_sha="mine", runs=runs)
    assert r.verdict == "isolated"
    assert r.signal is True
    assert "이 PR만 fail" in (r.summary_ko() or "")


def test_mixed_when_some_others_fail_below_threshold() -> None:
    # 1/4 others fail → not systemic, not all-pass → mixed, no decisive signal.
    runs = [
        _run("mine", "failure"),
        _run("o1", "failure"),
        _run("o2", "success"),
        _run("o3", "success"),
        _run("o4", "success"),
    ]
    r = analyze_cross_run(head_sha="mine", runs=runs)
    assert r.verdict == "mixed"
    assert r.signal is False


def test_inconclusive_when_too_few_other_prs() -> None:
    runs = [_run("mine", "failure"), _run("o1", "success")]
    r = analyze_cross_run(head_sha="mine", runs=runs)
    assert r.verdict == "inconclusive"
    assert r.signal is False
    assert r.summary_ko() is None


def test_reruns_of_one_pr_count_once() -> None:
    # 5 reruns of a single other PR (same head_sha) must not look like 5 PRs.
    runs = [_run("mine", "failure"), *(_run("other", "failure") for _ in range(5))]
    r = analyze_cross_run(head_sha="mine", runs=runs)
    assert r.others_total == 1  # deduped by head_sha
    assert r.verdict == "inconclusive"  # one distinct other PR < min_others


def test_no_head_sha_puts_all_runs_in_others() -> None:
    runs = [_run(f"o{i}", "failure") for i in range(3)]
    r = analyze_cross_run(head_sha=None, runs=runs)
    assert r.mine_total == 0
    assert r.others_total == 3 and r.others_failed == 3
    assert r.verdict == "systemic"
