"""Pure domain dataclasses for the PR review-comment auto-fix loop. Stdlib only.

Feature 004. The flow these types describe:

    FeedbackComment*   (what reviewers said — bots and humans alike)
        → TriageDecision*   (did Claude judge it worth fixing?)
            → FixOutcome    (what the agent actually changed, if anything)
                → one reply per comment (accepted AND rejected both get one)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Where a piece of feedback lives on the PR. The kind decides how we reply:
#   review_comment → POST /pulls/{n}/comments/{root_id}/replies  (in-thread)
#   review_body    → aggregated into one PR-level issue comment
#   issue_comment  → aggregated into one PR-level issue comment
CommentKind = Literal["review_comment", "issue_comment", "review_body"]

# `deferred` is the escape hatch for "legitimate point, but not mechanically
# fixable here" (needs a design decision, spans another PR, needs the author's
# intent). It gets a reply like `rejected` does, but says so honestly instead
# of pretending the point was wrong.
Verdict = Literal["accepted", "rejected", "deferred"]


@dataclass(frozen=True, slots=True)
class FeedbackComment:
    """One reviewer remark to triage.

    `path` / `line` / `diff_hunk` are populated for inline review comments and
    empty for PR-level ones — the prompt renders whichever it has.
    """

    comment_id: int
    kind: CommentKind
    author: str
    body: str
    created_at: str
    html_url: str = ""
    path: str | None = None
    line: int | None = None
    diff_hunk: str | None = None
    # Thread root for inline comments. GitHub's replies endpoint only accepts
    # a top-level review comment id, so a reply-to-a-reply must be redirected
    # to the root. Equals `comment_id` when this IS the root.
    reply_target_id: int | None = None


@dataclass(frozen=True, slots=True)
class TriageDecision:
    """Claude's verified judgement on one comment.

    `reasoning` is posted verbatim to GitHub — it is the operator-visible
    justification for either fixing or declining, so it must stand on its own.
    """

    comment_id: int
    verdict: Verdict
    reasoning: str
    # Free-form instruction handed to the fix agent. Only read for `accepted`.
    fix_instruction: str = ""
    # Optional file:line evidence the triage stage cited.
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceDiff:
    """`git diff --numstat` summary of what the fix agent changed."""

    changed_files: tuple[str, ...]
    insertions: int
    deletions: int

    @property
    def total_lines(self) -> int:
        return self.insertions + self.deletions

    @property
    def is_empty(self) -> bool:
        return not self.changed_files


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """Result of the repo's configured pre-push verification command."""

    command: str
    exit_code: int
    output_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class FixOutcome:
    """Everything the fix stage produced for one event."""

    summary: str
    diff: WorkspaceDiff
    commit_sha: str | None = None
    verify: VerifyOutcome | None = None


__all__ = [
    "CommentKind",
    "FeedbackComment",
    "FixOutcome",
    "TriageDecision",
    "Verdict",
    "VerifyOutcome",
    "WorkspaceDiff",
]
