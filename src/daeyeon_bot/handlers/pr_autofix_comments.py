"""Turn raw GitHub comment payloads into triageable `FeedbackComment`s.

Three GitHub surfaces carry review feedback on a PR, and a bot that reads only
one of them misses most of what reviewers actually said:

    /pulls/{n}/comments   inline review comments      → `review_comment`
    /pulls/{n}/reviews    the review's own body text  → `review_body`
    /issues/{n}/comments  PR-level conversation       → `issue_comment`

CodeRabbit, Copilot and friends put their summary findings in the review BODY
and only some of them inline; a human reviewer often just writes one
conversation comment. All three are folded into one stream here.

Filtering, in order (each step is a separate reason a comment is dropped, and
the order matters for the logs):
  1. the operator's own login — you don't answer your own notes. EXCEPT for
     this daemon's own `pr_review` output, which posts under that same identity
     and is identified by review id (`own_review_ids`); those findings are real
     feedback and are let through;
  2. our own prior replies, matched by `self_comment_markers`, because those
     are posted under the operator's `gh` identity and would otherwise read as
     fresh feedback on the next poll;
  3. `ignored_authors` globs (subtractive, wins over the allowlist);
  4. `comment_authors` globs (the allowlist);
  5. empty / whitespace-only bodies;
  6. GitHub's own "outdated" inline comments — the code they anchored to no
     longer exists in the diff, so acting on them is guesswork.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from daeyeon_bot.core.pr_autofix.types import FeedbackComment


# An inline comment whose anchor line vanished from the diff. GitHub keeps the
# thread but nulls `line`/`position`; `original_line` still points at the old
# code. Acting on one means guessing where it moved to, so we skip it.
def _is_outdated_inline(raw: dict[str, Any]) -> bool:
    return raw.get("line") is None and raw.get("position") is None


def _author_of(raw: dict[str, Any]) -> str:
    user = raw.get("user")
    if isinstance(user, dict):
        login = user.get("login")
        if isinstance(login, str):
            return login
    return ""


def _body_of(raw: dict[str, Any]) -> str:
    body = raw.get("body")
    return body.strip() if isinstance(body, str) else ""


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


@lru_cache(maxsize=256)
def _author_pattern(glob: str) -> re.Pattern[str]:
    """Compile an author glob honoring ONLY `*` and `?`.

    Deliberately not `fnmatch`. GitHub App logins literally end in `[bot]`, and
    under fnmatch's POSIX semantics `[bot]` is a CHARACTER CLASS matching one of
    b/o/t — so the single most natural setting an operator would write,
    `comment_authors = ["*[bot]"]`, silently matches nothing at all. Every
    character except `*` and `?` is escaped here, which makes brackets literal
    and the config mean what it looks like it means.
    """
    parts = [".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in glob]
    return re.compile("".join(parts) + r"\Z")


def author_matches(author: str, glob: str) -> bool:
    """Case-insensitive glob match for a GitHub login. See `_author_pattern`."""
    return _author_pattern(glob.lower()).match(author.lower()) is not None


def is_author_eligible(
    author: str,
    *,
    operator_login: str,
    allow_globs: list[str],
    ignore_globs: list[str],
) -> bool:
    """Author-level gate. Case-insensitive, since GitHub logins are.

    An EMPTY `allow_globs` admits nothing. That inverts `allowed_repos`'s
    empty-means-everything convention on purpose: clearing the author list is
    how an operator turns the feature off, and defaulting it open would be a
    surprising way to grant a pushing bot more reach.
    """
    if not author:
        return False
    if operator_login and author.lower() == operator_login.lower():
        return False
    if any(author_matches(author, g) for g in ignore_globs):
        return False
    if not allow_globs:
        return False
    return any(author_matches(author, g) for g in allow_globs)


def _is_own_reply(body: str, markers: list[str]) -> bool:
    """True if this comment is one the bot itself posted earlier."""
    return any(marker and marker in body for marker in markers)


def build_feedback(
    *,
    review_comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
    operator_login: str,
    allow_globs: list[str],
    ignore_globs: list[str],
    self_comment_markers: list[str],
    own_review_ids: frozenset[int] | set[int] = frozenset(),
) -> list[FeedbackComment]:
    """Merge and filter the three surfaces into one ordered feedback stream.

    Thread roots are resolved here, once, while we still hold every inline
    comment: GitHub's replies endpoint answers 422 for a reply-to-a-reply, so
    each comment carries the id we may actually POST to.
    """
    out: list[FeedbackComment] = []

    # Root resolution needs the whole inline set, including comments we will
    # then filter out (our own reply is frequently the parent of a reviewer's
    # follow-up), so build the map before filtering.
    root_of: dict[int, int] = {}
    for raw in review_comments:
        cid = _int_or_none(raw.get("id"))
        if cid is None:
            continue
        parent = _int_or_none(raw.get("in_reply_to_id"))
        root_of[cid] = cid if parent is None else root_of.get(parent, parent)

    def eligible(author: str, body: str, *, from_own_review: bool = False) -> bool:
        if not body or _is_own_reply(body, self_comment_markers):
            return False
        if from_own_review:
            # Our own `pr_review` output. It arrives under the operator's `gh`
            # identity, so the author gate below would drop it — which silently
            # made the bot's own findings the one class of feedback autofix
            # could never act on. The marker check above still stops it from
            # answering its OWN replies, which is the loop that actually needs
            # preventing.
            return True
        return is_author_eligible(
            author,
            operator_login=operator_login,
            allow_globs=allow_globs,
            ignore_globs=ignore_globs,
        )

    for raw in review_comments:
        cid = _int_or_none(raw.get("id"))
        if cid is None or _is_outdated_inline(raw):
            continue
        author, body = _author_of(raw), _body_of(raw)
        own = _int_or_none(raw.get("pull_request_review_id")) in own_review_ids
        if not eligible(author, body, from_own_review=own):
            continue
        out.append(
            FeedbackComment(
                comment_id=cid,
                kind="review_comment",
                author=author,
                body=body,
                created_at=str(raw.get("created_at") or ""),
                html_url=str(raw.get("html_url") or ""),
                path=raw.get("path") if isinstance(raw.get("path"), str) else None,
                line=_int_or_none(raw.get("line")),
                diff_hunk=raw.get("diff_hunk") if isinstance(raw.get("diff_hunk"), str) else None,
                reply_target_id=root_of.get(cid, cid),
            )
        )

    for raw in reviews:
        rid = _int_or_none(raw.get("id"))
        if rid is None:
            continue
        # A review that only carries inline comments has an empty body and is
        # already represented by those comments — no need for a duplicate item.
        author, body = _author_of(raw), _body_of(raw)
        if not eligible(author, body, from_own_review=rid in own_review_ids):
            continue
        out.append(
            FeedbackComment(
                comment_id=rid,
                kind="review_body",
                author=author,
                body=body,
                created_at=str(raw.get("submitted_at") or ""),
                html_url=str(raw.get("html_url") or ""),
            )
        )

    for raw in issue_comments:
        cid = _int_or_none(raw.get("id"))
        if cid is None:
            continue
        author, body = _author_of(raw), _body_of(raw)
        if not eligible(author, body):
            continue
        out.append(
            FeedbackComment(
                comment_id=cid,
                kind="issue_comment",
                author=author,
                body=body,
                created_at=str(raw.get("created_at") or ""),
                html_url=str(raw.get("html_url") or ""),
            )
        )

    out.sort(key=lambda c: (c.created_at, c.comment_id))
    return out


__all__ = ["author_matches", "build_feedback", "is_author_eligible"]
