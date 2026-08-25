"""Pydantic v2 schemas for the two Claude stages of `pr_autofix` (feature 004).

Stage 1 (triage) is JSON-validated because its output drives irreversible
action — an `accepted` verdict is what authorizes a git push. `extra="forbid"`
matters here for the same reason it does in `pr_review_schemas`: a hallucinated
key must never reach the code that acts on this object.

Stage 2 (the fix agent) is NOT schema-validated. It edits files with tools, and
the ground truth about what it did is `git diff`, not its own report. Its text
reply is used only as the human-readable summary in the reply comment.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Verdict = Literal["accepted", "rejected", "deferred"]


class TriageDecisionOut(BaseModel):
    """Claude's judgement on one reviewer comment."""

    model_config = {"extra": "forbid"}

    comment_id: int = Field(ge=1)
    verdict: Verdict
    # Posted verbatim to GitHub as the operator-visible justification, so it
    # must read as a complete thought, not a fragment.
    reasoning: str = Field(min_length=1, max_length=2000)
    # Only meaningful for `accepted`; the fix agent receives it as its brief.
    fix_instruction: str = Field(default="", max_length=4000)
    # Optional `file:line` or quoted-source evidence backing the judgement.
    evidence: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def accepted_needs_instruction(self) -> TriageDecisionOut:
        """An `accepted` verdict without a concrete instruction is not a decision.

        Without this gate the fix agent receives "yes, fix it" and has to
        re-derive the intent from the raw comment — which is exactly the
        re-interpretation step the triage stage exists to pin down.
        """
        if self.verdict == "accepted" and not self.fix_instruction.strip():
            raise ValueError("verdict=accepted requires a non-empty fix_instruction")
        return self


class TriageOutput(BaseModel):
    """Top-level triage reply: one decision per comment we asked about."""

    model_config = {"extra": "forbid"}

    decisions: list[TriageDecisionOut] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def no_duplicate_comment_ids(self) -> TriageOutput:
        """One decision per comment — a duplicate would make the reply pass
        post two contradictory answers into the same thread."""
        seen = {d.comment_id for d in self.decisions}
        if len(seen) != len(self.decisions):
            raise ValueError("decisions contain duplicate comment_id values")
        return self


__all__ = ["TriageDecisionOut", "TriageOutput", "Verdict"]
