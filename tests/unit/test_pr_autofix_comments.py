"""Feature 004 — which reviewer remarks become triageable feedback.

This filter is where the loop can go wrong in the most expensive way: if the
bot's own replies leak back in as "feedback", it triages them, replies to its
reply, and loops forever. Several tests below exist only to pin that down.
"""

from __future__ import annotations

from typing import Any

from daeyeon_bot.handlers.pr_autofix_comments import build_feedback, is_author_eligible

OPERATOR = "daeyeon-lee"


def _rc(
    cid: int,
    author: str,
    body: str,
    *,
    line: int | None = 10,
    in_reply_to: int | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": cid,
        "user": {"login": author},
        "body": body,
        "path": "src/app.py",
        "line": line,
        "position": line,
        "diff_hunk": "@@ -1 +1 @@",
        "created_at": created_at,
    }
    if in_reply_to is not None:
        item["in_reply_to_id"] = in_reply_to
    return item


def _build(**kwargs: Any) -> list[Any]:
    defaults: dict[str, Any] = {
        "review_comments": [],
        "reviews": [],
        "issue_comments": [],
        "operator_login": OPERATOR,
        "allow_globs": ["*"],
        "ignore_globs": [],
        "self_comment_markers": ["daeyeon-bot autofix"],
    }
    defaults.update(kwargs)
    return build_feedback(**defaults)


# ── author gate ───────────────────────────────────────────────────────────


def test_operator_own_comments_are_never_feedback() -> None:
    out = _build(review_comments=[_rc(1, OPERATOR, "note to self")])
    assert out == []


def test_operator_match_is_case_insensitive() -> None:
    """GitHub logins are case-insensitive; a `Daeyeon-Lee` self-comment must not
    slip through and get answered by the bot."""
    out = _build(review_comments=[_rc(1, "Daeyeon-Lee", "note to self")])
    assert out == []


def test_bot_and_human_both_pass_the_default_allowlist() -> None:
    out = _build(
        review_comments=[
            _rc(1, "coderabbitai[bot]", "this will crash on None"),
            _rc(2, "some-colleague", "왜 이렇게 했어요?"),
        ]
    )
    assert [c.comment_id for c in out] == [1, 2]


def test_bot_only_allowlist_drops_humans() -> None:
    out = _build(
        review_comments=[
            _rc(1, "coderabbitai[bot]", "bot finding"),
            _rc(2, "some-colleague", "human note"),
        ],
        allow_globs=["*[bot]"],
    )
    assert [c.author for c in out] == ["coderabbitai[bot]"]


def test_ignored_authors_win_over_the_allowlist() -> None:
    out = _build(
        review_comments=[_rc(1, "noisy-bot[bot]", "nit")],
        allow_globs=["*"],
        ignore_globs=["noisy-bot[bot]"],
    )
    assert out == []


def test_empty_allowlist_admits_nothing() -> None:
    """An operator who clears `comment_authors` has switched the feature off,
    not opened it up — the opposite of `allowed_repos`'s empty-means-all."""
    out = _build(review_comments=[_rc(1, "coderabbitai[bot]", "x")], allow_globs=[])
    assert out == []


def test_is_author_eligible_rejects_blank_author() -> None:
    assert not is_author_eligible("", operator_login=OPERATOR, allow_globs=["*"], ignore_globs=[])


# ── the bot's own replies must not re-enter ───────────────────────────────


def test_bot_own_reply_is_not_treated_as_new_feedback() -> None:
    """The reply is posted under the OPERATOR's gh identity in production, but a
    marker match must hold even for a comment attributed to someone else — this
    is the guard that stops an infinite reply-to-my-reply loop."""
    out = _build(
        review_comments=[
            _rc(1, "helper-account", "🤖 **daeyeon-bot autofix** — 수정했습니다\n\n...")
        ]
    )
    assert out == []


def test_reviewer_pushback_after_a_bot_reply_is_new_feedback() -> None:
    """The whole point of the loop resuming: a human disagreeing with the bot's
    rejection is a NEW comment id and must come back through."""
    out = _build(
        review_comments=[
            _rc(1, "coderabbitai[bot]", "original finding"),
            _rc(2, OPERATOR, "🤖 **daeyeon-bot autofix** — 수정하지 않았습니다", in_reply_to=1),
            _rc(3, "some-colleague", "아니요, 이건 진짜 버그예요", in_reply_to=2),
        ]
    )
    assert [c.comment_id for c in out] == [1, 3]


# ── thread-root resolution ────────────────────────────────────────────────


def test_reply_target_is_the_thread_root_not_the_parent() -> None:
    """GitHub's replies endpoint 422s on a reply-to-a-reply, so a comment three
    deep must still resolve back to the root id."""
    out = _build(
        review_comments=[
            _rc(1, "coderabbitai[bot]", "root"),
            _rc(2, OPERATOR, "mine", in_reply_to=1),
            _rc(3, "coderabbitai[bot]", "still broken", in_reply_to=2),
        ]
    )
    deep = next(c for c in out if c.comment_id == 3)
    assert deep.reply_target_id == 1


def test_root_comment_targets_itself() -> None:
    out = _build(review_comments=[_rc(7, "coderabbitai[bot]", "finding")])
    assert out[0].reply_target_id == 7


# ── content gates ─────────────────────────────────────────────────────────


def test_outdated_inline_comment_is_skipped() -> None:
    """`line` and `position` both null means the anchored code left the diff.
    Acting on it would be guesswork about where it moved."""
    out = _build(review_comments=[_rc(1, "coderabbitai[bot]", "stale", line=None)])
    assert out == []


def test_empty_body_is_skipped() -> None:
    out = _build(review_comments=[_rc(1, "coderabbitai[bot]", "   \n  ")])
    assert out == []


def test_review_with_empty_body_is_not_duplicated() -> None:
    """A review that only carries inline comments has an empty body; those
    comments already represent it."""
    out = _build(
        reviews=[{"id": 99, "user": {"login": "coderabbitai[bot]"}, "body": ""}],
        review_comments=[_rc(1, "coderabbitai[bot]", "inline finding")],
    )
    assert [c.kind for c in out] == ["review_comment"]


# ── surface merge ─────────────────────────────────────────────────────────


def test_all_three_surfaces_are_merged_and_sorted_by_time() -> None:
    out = _build(
        review_comments=[_rc(3, "coderabbitai[bot]", "inline", created_at="2026-01-03T00:00:00Z")],
        reviews=[
            {
                "id": 2,
                "user": {"login": "copilot[bot]"},
                "body": "summary finding",
                "submitted_at": "2026-01-02T00:00:00Z",
            }
        ],
        issue_comments=[
            {
                "id": 1,
                "user": {"login": "some-colleague"},
                "body": "conversation note",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    assert [(c.comment_id, c.kind) for c in out] == [
        (1, "issue_comment"),
        (2, "review_body"),
        (3, "review_comment"),
    ]


def test_inline_comment_carries_its_diff_context() -> None:
    out = _build(review_comments=[_rc(1, "coderabbitai[bot]", "finding")])
    assert out[0].path == "src/app.py"
    assert out[0].line == 10
    assert out[0].diff_hunk == "@@ -1 +1 @@"


# ── glob semantics ────────────────────────────────────────────────────────


def test_bracket_in_glob_is_literal_not_a_character_class() -> None:
    """Regression guard. Under `fnmatch`, `*[bot]` is a character class matching
    ONE of b/o/t, so it matches nothing ending in `]` — meaning the single most
    natural config an operator would write would silently filter everything out.
    """
    from daeyeon_bot.handlers.pr_autofix_comments import author_matches

    assert author_matches("coderabbitai[bot]", "*[bot]")
    assert not author_matches("some-colleague", "*[bot]")
    assert not author_matches("coderabbitaib", "*[bot]")


def test_question_mark_matches_exactly_one_char() -> None:
    from daeyeon_bot.handlers.pr_autofix_comments import author_matches

    assert author_matches("bot1", "bot?")
    assert not author_matches("bot12", "bot?")


def test_glob_is_anchored_at_both_ends() -> None:
    """`coderabbit` must not match a login that merely CONTAINS it — an
    unanchored author filter would be a quiet privilege escalation."""
    from daeyeon_bot.handlers.pr_autofix_comments import author_matches

    assert author_matches("coderabbitai", "coderabbit*")
    assert not author_matches("evil-coderabbitai", "coderabbit*")
