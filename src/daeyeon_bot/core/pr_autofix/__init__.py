"""Domain types for the PR review-comment auto-fix feature (004)."""

from daeyeon_bot.core.pr_autofix.types import (
    CommentKind,
    FeedbackComment,
    FixOutcome,
    TriageDecision,
    Verdict,
    VerifyOutcome,
    WorkspaceDiff,
)

__all__ = [
    "CommentKind",
    "FeedbackComment",
    "FixOutcome",
    "TriageDecision",
    "Verdict",
    "VerifyOutcome",
    "WorkspaceDiff",
]
