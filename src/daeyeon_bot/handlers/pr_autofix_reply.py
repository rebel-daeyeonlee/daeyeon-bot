"""Reply rendering for `pr_autofix` (feature 004).

Every triaged comment gets an answer — that is the operator's explicit
requirement, and it is also what keeps the loop honest: an unanswered comment
is indistinguishable from one the bot never saw.

Where the answer goes depends on the comment's kind:

    review_comment → in-thread reply on its thread ROOT, one POST each. The
                     reviewer sees it next to the code they commented on.
    review_body    ┐ aggregated into ONE PR-level conversation comment. These
    issue_comment  ┘ have no thread to reply into, and N separate top-level
                     comments would bury the PR under bot noise.

Every body opens with `_MARKER`. That string is what
`pr_autofix_comments.build_feedback` matches to recognize the bot's own past
replies — they are posted under the operator's `gh` identity, so without the
marker the next poll would read them as fresh feedback and triage them.
"""

from __future__ import annotations

from daeyeon_bot.core.pr_autofix.types import (
    FeedbackComment,
    FixOutcome,
    TriageDecision,
    VerifyOutcome,
)

# Load-bearing: also the self-recognition marker. Keep in sync with
# `[handlers.pr_autofix].self_comment_markers`.
_MARKER = "daeyeon-bot autofix"

_HEADERS = {
    "accepted": f"🤖 **{_MARKER}** — 수정했습니다",
    # `push_enabled = false`. A fix exists, but only inside a throwaway clone
    # that the next round resets — so this must never read as "수정했습니다",
    # and must never carry a commit link (it would 404).
    "dry_run": f"🤖 **{_MARKER}** — 수정안만 만들었습니다 (dry run — push 안 함)",
    "rejected": f"🤖 **{_MARKER}** — 수정하지 않았습니다",
    "deferred": f"🤖 **{_MARKER}** — 사람 확인이 필요합니다",
    "failed": f"🤖 **{_MARKER}** — 처리하지 못했습니다",
}

_MAX_FILES_LISTED = 12
_MAX_VERIFY_TAIL = 1500


def _commit_line(commit_sha: str | None, repo: str) -> str:
    if not commit_sha:
        return ""
    return f"- 커밋: [`{commit_sha[:8]}`](https://github.com/{repo}/commit/{commit_sha})"


def _files_line(files: tuple[str, ...]) -> str:
    if not files:
        return ""
    shown = ", ".join(f"`{f}`" for f in files[:_MAX_FILES_LISTED])
    if len(files) > _MAX_FILES_LISTED:
        shown += f" 외 {len(files) - _MAX_FILES_LISTED}개"
    return f"- 변경 파일: {shown}"


def render_decision_reply(  # noqa: PLR0912 — one branch per reply shape; each is documented
    decision: TriageDecision,
    *,
    repo: str,
    outcome: FixOutcome | None,
    commit_sha: str | None,
    dry_run: bool = False,
) -> str:
    """The reply body for one triaged comment.

    Three outcomes have to stay distinguishable, because a reviewer reads this
    and decides whether to look:
      * fixed and shipped      → 수정했습니다, with a commit link;
      * fixed but NOT shipped  → dry run; says so, and links nothing, since the
        commit lives only in a workspace clone the next round wipes;
      * accepted but no fix    → 처리하지 못했습니다.
    Claiming a fix that is not on the branch is worse than admitting the miss —
    the reviewer stops checking.
    """
    verdict = decision.verdict
    if verdict == "accepted" and dry_run:
        lines = [
            _HEADERS["dry_run"],
            "",
            decision.reasoning,
            "",
            f"- 적용 예정: {decision.fix_instruction}" if decision.fix_instruction else "",
        ]
        if outcome is not None:
            files = _files_line(outcome.diff.changed_files)
            if files:
                lines.append(files)
            verify = _verify_line(outcome.verify)
            if verify:
                lines.append(verify)
        lines += [
            "",
            "_`push_enabled = false` 이라 실제 브랜치에는 아무것도 올리지 않았습니다._"
            " 적용을 원하시면 설정을 켠 뒤 이 스레드에 다시 코멘트해 주세요 —"
            " 다음 폴링에서 새 지적으로 다시 처리합니다.",
        ]
        return "\n".join(line for line in lines if line != "")
    if verdict == "accepted" and not commit_sha:
        lines = [
            _HEADERS["failed"],
            "",
            "지적은 타당하다고 판단했지만, 이번 라운드에서 실제 코드 변경으로 이어지지"
            " 못했습니다. 사람 확인이 필요합니다.",
            "",
            f"- 판단 근거: {decision.reasoning}",
        ]
        if decision.fix_instruction:
            lines.append(f"- 시도한 수정: {decision.fix_instruction}")
        return "\n".join(lines)

    lines = [_HEADERS.get(verdict, _HEADERS["failed"]), ""]

    if verdict == "accepted":
        lines.append(decision.reasoning)
        lines.append("")
        if decision.fix_instruction:
            lines.append(f"- 적용: {decision.fix_instruction}")
        commit = _commit_line(commit_sha, repo)
        if commit:
            lines.append(commit)
        if outcome is not None:
            files = _files_line(outcome.diff.changed_files)
            if files:
                lines.append(files)
            verify = _verify_line(outcome.verify)
            if verify:
                lines.append(verify)
    elif verdict == "rejected":
        lines.append("아래 이유로 이번 지적은 반영하지 않았습니다.")
        lines.append("")
        lines.append(f"**사유**: {decision.reasoning}")
        if decision.evidence:
            lines.append("")
            lines.append(f"**근거**: {decision.evidence}")
        lines.append("")
        lines.append(
            "_판단이 틀렸다면 이 스레드에 다시 코멘트해 주세요 — 다음 폴링에서 재검토합니다._"
        )
    else:  # deferred
        lines.append("타당한 지적이지만 자동으로 수정하기에는 판단이 필요합니다.")
        lines.append("")
        lines.append(f"**사유**: {decision.reasoning}")
        if decision.evidence:
            lines.append("")
            lines.append(f"**근거**: {decision.evidence}")

    return "\n".join(lines)


def _verify_line(verify: VerifyOutcome | None) -> str:
    if verify is None:
        return ""
    status = "통과" if verify.passed else f"실패 (exit {verify.exit_code})"
    return f"- 검증: `{verify.command}` → {status}"


def render_aggregate_comment(
    items: list[tuple[TriageDecision, FeedbackComment]],
    *,
    repo: str,
    outcome: FixOutcome | None,
    commit_sha: str | None,
    dry_run: bool = False,
) -> str:
    """One PR-level comment answering every non-inline feedback item."""
    lines = [f"🤖 **{_MARKER}**", ""]
    for decision, comment in items:
        label = {
            "accepted": "✅ 수정함",
            "rejected": "🚫 수정 안 함",
            "deferred": "🤔 사람 확인 필요",
        }.get(decision.verdict, "⚠️ 처리 실패")
        if decision.verdict == "accepted" and dry_run:
            label = "📝 수정안만 (dry run)"
        elif decision.verdict == "accepted" and not commit_sha:
            label = "⚠️ 미적용"
        source = (
            f"[`{comment.author}`의 코멘트]({comment.html_url})"
            if comment.html_url
            else f"`{comment.author}`의 코멘트"
        )
        lines.append(f"### {label} — {source}")
        lines.append("")
        lines.append(f"> {_quote(comment.body)}")
        lines.append("")
        lines.append(decision.reasoning)
        if decision.verdict == "accepted" and decision.fix_instruction:
            lines.append("")
            lines.append(f"- 적용: {decision.fix_instruction}")
        lines.append("")

    if dry_run:
        lines.append("---")
        lines.append("_`push_enabled = false` — 실제 브랜치에는 아무것도 올리지 않았습니다._")
        return "\n".join(lines)
    commit = _commit_line(commit_sha, repo)
    if commit:
        lines.append("---")
        lines.append(commit)
        if outcome is not None:
            files = _files_line(outcome.diff.changed_files)
            if files:
                lines.append(files)
            verify = _verify_line(outcome.verify)
            if verify:
                lines.append(verify)
    return "\n".join(lines)


def render_verify_failed_comment(*, repo: str, verify: VerifyOutcome, accepted_count: int) -> str:
    """Posted when the fix landed in the tree but the pre-push check failed.

    Nothing was pushed. The output tail is included because the operator's next
    question is always "실패한 게 뭔데?", and making them re-run it locally to
    find out defeats the point of running it here.
    """
    del repo
    return "\n".join(
        [
            f"🤖 **{_MARKER}** — 검증 실패로 push하지 않았습니다",
            "",
            f"리뷰 지적 {accepted_count}건을 수정했지만, push 전 검증 명령이 실패해서"
            " 커밋하지 않고 되돌렸습니다.",
            "",
            f"- 명령: `{verify.command}`",
            f"- 종료 코드: `{verify.exit_code}`",
            "",
            "<details><summary>출력 (tail)</summary>",
            "",
            "```",
            verify.output_tail[-_MAX_VERIFY_TAIL:],
            "```",
            "",
            "</details>",
            "",
            "_수동으로 확인이 필요합니다. 이 스레드의 지적들은 다음 폴링에서 다시 시도됩니다._",
        ]
    )


def render_max_rounds_comment(*, rounds: int) -> str:
    """Posted once when a PR exhausts its autofix round budget."""
    return "\n".join(
        [
            f"🤖 **{_MARKER}** — 자동 수정 라운드를 모두 소진했습니다 ({rounds}회)",
            "",
            "리뷰 지적이 계속 올라오고 있지만, 자동 수정을 여기서 멈춥니다."
            " 봇끼리 무한히 주고받는 것을 막기 위한 상한입니다.",
            "",
            "남은 지적은 사람이 확인해 주세요."
            " 상한은 `[handlers.pr_autofix].max_rounds` 로 조정합니다.",
        ]
    )


def _quote(body: str, *, limit: int = 300) -> str:
    """First meaningful line of a comment, flattened for a markdown blockquote."""
    for raw in body.splitlines():
        line = raw.strip()
        if line and not line.startswith(("<!--", "|", "---")):
            return (line[:limit] + "…") if len(line) > limit else line
    return (body[:limit] + "…") if len(body) > limit else body


__all__ = [
    "render_aggregate_comment",
    "render_decision_reply",
    "render_max_rounds_comment",
    "render_verify_failed_comment",
]
