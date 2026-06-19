"""Claude triage output contract (`TriageOutput`, Pydantic v2) — feature 003.

Output language is Korean prose + English technical terms (set by the persona);
this module only validates structure. `owner_area` equals the OnCall-wiki
`domain` frontmatter vocabulary (drift-guarded by a test against the pinned list
in contracts/oncall-wiki-surface.md). See specs/003-ci-monitor-bot/plan.md
§Output schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Attribution = Literal["infra_env", "product_regression", "flaky", "unknown"]
Classification = Literal[
    "infra",
    "environment",
    "test_failure",
    "device_failure",
    "build_failure",
    "dependency",
    "timeout",
    "flaky",
    "permission",
    "unknown",
]
# == ssw-devops-oncall vault `domain:` frontmatter vocabulary (+ Unknown).
OwnerArea = Literal["DevOps", "SysFw", "SysSol", "Connectivity", "Driver", "HW", "Unknown"]
Confidence = Literal["low", "medium", "high"]
RerunAdvice = Literal["safe_to_rerun", "do_not_rerun", "needs_investigation", "unknown"]


class Evidence(BaseModel):
    """One log-grounded evidence bullet."""

    model_config = ConfigDict(extra="forbid")
    quote: str = Field(min_length=1)  # a real line from the failed log / Loki slice
    citation: str = Field(min_length=1)  # where it came from (job/step/log source)


class WikiRef(BaseModel):
    """One OnCall-wiki incident / playbook reference."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    why: str = Field(min_length=1)


class TriageOutput(BaseModel):
    """Structured first-pass triage Claude must return (parsed from its reply)."""

    model_config = ConfigDict(extra="forbid")

    attribution: Attribution
    classification: Classification
    owner_area: OwnerArea
    confidence: Confidence
    summary: str = Field(min_length=1)
    log_evidence: tuple[Evidence, ...] = ()
    wiki_matches: tuple[WikiRef, ...] = ()
    likely_cause: str = Field(min_length=1)
    known_remedy: str | None = None
    recommended_action: str = Field(min_length=1)
    rerun_advice: RerunAdvice
    needs_human: bool

    @model_validator(mode="after")
    def _check(self) -> TriageOutput:
        # (1) evidence required when a definite attribution is claimed.
        if self.attribution != "unknown" and not self.log_evidence:
            raise ValueError("log_evidence is required when attribution is not 'unknown'")
        # (2) high confidence needs at least one piece of grounding.
        if self.confidence == "high" and not self.log_evidence and not self.wiki_matches:
            raise ValueError("confidence 'high' requires at least one log_evidence or wiki match")
        return self


def enforce_confidence_floor(
    out: TriageOutput,
    *,
    has_strong_anchor: bool,
    has_wiki_match: bool,
) -> TriageOutput:
    """Cap confidence by anchor strength (plan §Output schema rule 2).

    With no wiki match AND only weak anchors, confidence cannot exceed `low`; a
    single strong signature match permits `medium`. The handler computes anchor
    strength (it knows whether a wiki `signature:` matched), so this lives here
    rather than in the model validator. Returns a copy with capped confidence."""
    if has_wiki_match and has_strong_anchor:
        return out  # full range allowed
    if has_strong_anchor or has_wiki_match:
        ceiling: Confidence = "medium"
    else:
        ceiling = "low"
    order = {"low": 0, "medium": 1, "high": 2}
    if order[out.confidence] <= order[ceiling]:
        return out
    return out.model_copy(update={"confidence": ceiling})


__all__ = [
    "Attribution",
    "Classification",
    "Confidence",
    "Evidence",
    "OwnerArea",
    "RerunAdvice",
    "TriageOutput",
    "WikiRef",
    "enforce_confidence_floor",
]
