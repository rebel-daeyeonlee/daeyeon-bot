"""Feature 004 — every triaged comment gets an answer, and the answer is honest.

The operator's requirement was explicit: reply when you fixed it, AND reply when
you refused. The tests that matter most here are the ones about NOT overclaiming
— an "accepted" verdict whose fix never made it into a commit must not read as a
success, or reviewers stop double-checking the bot.
"""

from __future__ import annotations

from daeyeon_bot.core.pr_autofix.types import (
    FeedbackComment,
    FixOutcome,
    TriageDecision,
    VerifyOutcome,
    WorkspaceDiff,
)
from daeyeon_bot.handlers.pr_autofix_reply import (
    render_aggregate_comment,
    render_decision_reply,
    render_max_rounds_comment,
    render_verify_failed_comment,
)

REPO = "rebel-daeyeonlee/daeyeon-bot"
SHA = "abc1234def5678"

_DIFF = WorkspaceDiff(changed_files=("src/app.py", "tests/test_app.py"), insertions=8, deletions=3)


def _decision(verdict: str, **kw: str) -> TriageDecision:
    base = {
        "comment_id": 1,
        "verdict": verdict,
        "reasoning": "근거 문장.",
        "fix_instruction": "`app.py:12`의 경계 조건을 고칩니다.",
        "evidence": "src/app.py:12",
    }
    base.update(kw)
    return TriageDecision(**base)  # type: ignore[arg-type]


def _comment(kind: str = "review_comment") -> FeedbackComment:
    return FeedbackComment(
        comment_id=1,
        kind=kind,  # type: ignore[arg-type]
        author="coderabbitai[bot]",
        body="This will crash when `items` is empty.\n\n_Committable suggestion_",
        created_at="2026-01-01T00:00:00Z",
        html_url=f"https://github.com/{REPO}/pull/9#discussion_r1",
    )


# ── the self-recognition marker ───────────────────────────────────────────


def test_every_reply_carries_the_self_recognition_marker() -> None:
    """Replies are posted under the operator's own gh identity. Without this
    marker in the body, the next poll reads them as fresh feedback and the bot
    answers itself forever."""
    outcome = FixOutcome(summary="", diff=_DIFF, commit_sha=SHA)
    bodies = [
        render_decision_reply(_decision("accepted"), repo=REPO, outcome=outcome, commit_sha=SHA),
        render_decision_reply(_decision("rejected"), repo=REPO, outcome=None, commit_sha=None),
        render_decision_reply(_decision("deferred"), repo=REPO, outcome=None, commit_sha=None),
        render_aggregate_comment(
            [(_decision("rejected"), _comment("issue_comment"))],
            repo=REPO,
            outcome=None,
            commit_sha=None,
        ),
        render_verify_failed_comment(
            repo=REPO,
            verify=VerifyOutcome(command="just check", exit_code=1, output_tail="boom"),
            accepted_count=2,
        ),
        render_max_rounds_comment(rounds=5),
    ]
    assert all("daeyeon-bot autofix" in body for body in bodies)


# ── accepted ──────────────────────────────────────────────────────────────


def test_accepted_reply_names_the_commit_and_files() -> None:
    outcome = FixOutcome(summary="", diff=_DIFF, commit_sha=SHA)
    body = render_decision_reply(_decision("accepted"), repo=REPO, outcome=outcome, commit_sha=SHA)
    assert "수정했습니다" in body
    assert SHA[:8] in body
    assert f"https://github.com/{REPO}/commit/{SHA}" in body
    assert "`src/app.py`" in body


def test_accepted_without_a_commit_does_not_claim_success() -> None:
    """The single most important honesty guard in this module."""
    outcome = FixOutcome(summary="", diff=WorkspaceDiff((), 0, 0))
    body = render_decision_reply(_decision("accepted"), repo=REPO, outcome=outcome, commit_sha=None)
    assert "수정했습니다" not in body
    assert "처리하지 못했습니다" in body
    assert "사람 확인이 필요합니다" in body


def test_accepted_reply_reports_the_verify_result() -> None:
    outcome = FixOutcome(
        summary="",
        diff=_DIFF,
        commit_sha=SHA,
        verify=VerifyOutcome(command="just check", exit_code=0, output_tail="ok"),
    )
    body = render_decision_reply(_decision("accepted"), repo=REPO, outcome=outcome, commit_sha=SHA)
    assert "`just check` → 통과" in body


# ── rejected / deferred ───────────────────────────────────────────────────


def test_rejected_reply_carries_reason_evidence_and_an_invitation_to_push_back() -> None:
    body = render_decision_reply(
        _decision("rejected", reasoning="`x.py:12`에서 이미 검사합니다."),
        repo=REPO,
        outcome=None,
        commit_sha=None,
    )
    assert "수정하지 않았습니다" in body
    assert "`x.py:12`에서 이미 검사합니다." in body
    assert "src/app.py:12" in body  # evidence
    assert "다시 코멘트" in body  # the loop can resume


def test_deferred_reply_asks_for_a_human_rather_than_declaring_a_verdict() -> None:
    body = render_decision_reply(_decision("deferred"), repo=REPO, outcome=None, commit_sha=None)
    assert "사람 확인이 필요합니다" in body
    assert "수정하지 않았습니다" not in body


# ── aggregate (non-inline) ────────────────────────────────────────────────


def test_aggregate_comment_folds_every_non_inline_item_into_one_post() -> None:
    items = [
        (_decision("accepted"), _comment("review_body")),
        (
            TriageDecision(
                comment_id=2, verdict="rejected", reasoning="범위 밖입니다.", fix_instruction=""
            ),
            FeedbackComment(
                comment_id=2,
                kind="issue_comment",
                author="some-colleague",
                body="스타일 통일 부탁해요",
                created_at="2026-01-02T00:00:00Z",
            ),
        ),
    ]
    body = render_aggregate_comment(items, repo=REPO, outcome=None, commit_sha=SHA)
    assert "✅ 수정함" in body
    assert "🚫 수정 안 함" in body
    assert "범위 밖입니다." in body
    assert SHA[:8] in body


def test_aggregate_marks_an_accepted_item_unapplied_when_nothing_was_committed() -> None:
    body = render_aggregate_comment(
        [(_decision("accepted"), _comment("review_body"))],
        repo=REPO,
        outcome=None,
        commit_sha=None,
    )
    assert "⚠️ 미적용" in body
    assert "✅ 수정함" not in body


def test_aggregate_quote_skips_html_comments_and_table_rows() -> None:
    """Review bots open with `<!-- ... -->` markers and markdown tables; quoting
    those makes the reply unreadable."""
    comment = FeedbackComment(
        comment_id=3,
        kind="issue_comment",
        author="coderabbitai[bot]",
        body="<!-- walkthrough -->\n| a | b |\n---\n실제 지적 내용입니다",
        created_at="2026-01-01T00:00:00Z",
    )
    body = render_aggregate_comment(
        [(_decision("rejected"), comment)], repo=REPO, outcome=None, commit_sha=None
    )
    assert "> 실제 지적 내용입니다" in body


# ── verify failure / round exhaustion ─────────────────────────────────────


def test_verify_failed_comment_shows_the_command_and_output() -> None:
    body = render_verify_failed_comment(
        repo=REPO,
        verify=VerifyOutcome(command="just check", exit_code=2, output_tail="E   assert 1 == 2"),
        accepted_count=3,
    )
    assert "push하지 않았습니다" in body
    assert "`just check`" in body
    assert "`2`" in body
    assert "assert 1 == 2" in body
    assert "다시 시도됩니다" in body


def test_max_rounds_comment_names_the_knob_that_controls_it() -> None:
    body = render_max_rounds_comment(rounds=5)
    assert "5회" in body
    assert "max_rounds" in body
