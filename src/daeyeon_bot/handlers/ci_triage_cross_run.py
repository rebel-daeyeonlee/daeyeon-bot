"""Cross-run comparison — turn "다른 PR도 fail하나?" into a numbered signal.

Pure analysis (no I/O): given the failing run's `head_sha` and a list of recent
COMPLETED runs of the same workflow, decide whether the failure looks SYSTEMIC
(other PRs fail it too → infra_env) or ISOLATED (other PRs pass, only this PR
fails → product_regression). This is exactly the move on-call makes by hand
("다른 PR들은 통과하고 있나요?"); here it is evidenced with raw counts so the
verdict is auditable, never a guess. Insufficient data → `inconclusive`, which
the handler maps to lower confidence rather than a fabricated attribution.

See specs/003-ci-monitor-bot/plan.md §P1 cross-run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

CrossRunVerdict = Literal["systemic", "isolated", "mixed", "inconclusive"]

# Run-level conclusions that count as "this run did not pass". `cancelled` is
# included because a cancelled premerge on a shared DUT is usually infra (host
# yanked / superseded), not a clean pass.
_FAIL_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "startup_failure", "stale", "action_required"}
)


class _RunLike(Protocol):
    # read-only properties so a frozen dataclass (RunSummary) satisfies the protocol.
    @property
    def head_sha(self) -> str | None: ...
    @property
    def conclusion(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class CrossRunResult:
    """Outcome of the comparison. Counts are over DISTINCT other PRs (deduped by
    head_sha) so a flurry of reruns of one PR can't masquerade as many."""

    verdict: CrossRunVerdict
    others_total: int
    others_failed: int
    mine_total: int
    mine_failed: int

    @property
    def signal(self) -> bool:
        """True when the comparison actually distinguishes infra vs regression —
        the handler lets this lift the confidence floor to `medium`."""
        return self.verdict in ("systemic", "isolated")

    def summary_ko(self) -> str | None:
        """The one-line `🔬` Slack/prompt context, or None when inconclusive with
        no usable comparison (nothing worth a line)."""
        if self.verdict == "systemic":
            return (
                f"🔬 동일 workflow 최근 다른 PR {self.others_total}건 중 "
                f"{self.others_failed}건 fail → 환경·인프라 유력"
            )
        if self.verdict == "isolated":
            return f"🔬 최근 다른 PR {self.others_total}건 모두 통과, 이 PR만 fail → 코드 회귀 유력"
        if self.verdict == "mixed":
            return f"🔬 동일 workflow 최근 다른 PR {self.others_failed}/{self.others_total} fail (혼재)"
        return None

    def prompt_block(self) -> str:
        """Compact evidence block for the Claude prompt (raw counts + hint)."""
        return (
            "## Cross-run comparison (same workflow, recent completed runs; run-level)\n"
            f"- this PR (same head_sha): {self.mine_failed}/{self.mine_total} failed\n"
            f"- other recent PRs (distinct head_sha): {self.others_failed}/{self.others_total} failed\n"
            f"- verdict hint: {self.verdict}\n"
            "Use this to decide infra_env vs product_regression: other PRs also failing"
            " broadly → infra_env; only this PR fails while others pass → product_regression."
            " Do NOT claim either beyond what this + the log support; otherwise attribution=unknown."
        )


def analyze_cross_run(
    *,
    head_sha: str | None,
    runs: Sequence[_RunLike],
    min_others: int = 3,
) -> CrossRunResult:
    """Classify the failure against sibling runs.

    `runs` is recent completed runs of the same workflow (newest first); the
    failing run itself may be included. `min_others` distinct other PRs are
    required before a SYSTEMIC/ISOLATED verdict — below that the sample is too
    small to be evidence, so the result is `inconclusive`."""
    mine = [r for r in runs if head_sha and r.head_sha == head_sha]
    mine_total = len(mine)
    mine_failed = sum(1 for r in mine if (r.conclusion or "") in _FAIL_CONCLUSIONS)

    # Dedupe other PRs by head_sha so reruns of one PR count once.
    seen: set[str] = set()
    others: list[_RunLike] = []
    for r in runs:
        if head_sha and r.head_sha == head_sha:
            continue
        key = r.head_sha or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        others.append(r)
    others_total = len(others)
    others_failed = sum(1 for r in others if (r.conclusion or "") in _FAIL_CONCLUSIONS)

    verdict: CrossRunVerdict
    if others_total < min_others:
        verdict = "inconclusive"
    elif others_failed >= 2 and others_failed / others_total >= 0.4:
        verdict = "systemic"
    elif others_failed == 0 and mine_failed >= 1:
        verdict = "isolated"
    else:
        verdict = "mixed"

    return CrossRunResult(
        verdict=verdict,
        others_total=others_total,
        others_failed=others_failed,
        mine_total=mine_total,
        mine_failed=mine_failed,
    )


__all__ = ["CrossRunResult", "CrossRunVerdict", "analyze_cross_run"]
