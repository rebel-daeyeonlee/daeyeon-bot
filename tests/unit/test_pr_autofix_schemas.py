"""Feature 004 — the triage schema is what authorizes a push, so it validates."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daeyeon_bot.handlers.pr_autofix_schemas import TriageOutput


def _payload(**overrides: object) -> dict[str, object]:
    decision: dict[str, object] = {
        "comment_id": 1,
        "verdict": "accepted",
        "reasoning": "리뷰어 지적이 맞습니다.",
        "fix_instruction": "app.py:12의 `>`를 `>=`로 바꿉니다.",
    }
    decision.update(overrides)
    return {"decisions": [decision]}


def test_accepts_a_well_formed_decision() -> None:
    out = TriageOutput.model_validate(_payload())
    assert out.decisions[0].verdict == "accepted"


def test_accepted_without_a_fix_instruction_is_rejected() -> None:
    """'yes, fix it' is not a decision — the fix agent would have to re-derive
    the intent, which is the step triage exists to pin down."""
    with pytest.raises(ValidationError, match="fix_instruction"):
        TriageOutput.model_validate(_payload(fix_instruction=""))


def test_accepted_with_whitespace_only_instruction_is_rejected() -> None:
    with pytest.raises(ValidationError, match="fix_instruction"):
        TriageOutput.model_validate(_payload(fix_instruction="   \n "))


def test_rejected_needs_no_fix_instruction() -> None:
    out = TriageOutput.model_validate(
        _payload(verdict="rejected", fix_instruction="", reasoning="이미 상위에서 검사합니다.")
    )
    assert out.decisions[0].verdict == "rejected"


def test_duplicate_comment_ids_are_rejected() -> None:
    """Two verdicts for one comment would post contradictory replies into the
    same thread."""
    payload = _payload()
    payload["decisions"] = [payload["decisions"][0], dict(payload["decisions"][0])]  # type: ignore[index]
    with pytest.raises(ValidationError, match="duplicate"):
        TriageOutput.model_validate(payload)


def test_unknown_key_is_rejected() -> None:
    """`extra="forbid"` — a hallucinated key must not reach code that pushes."""
    with pytest.raises(ValidationError):
        TriageOutput.model_validate(_payload(auto_merge=True))


def test_unknown_verdict_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TriageOutput.model_validate(_payload(verdict="probably"))


def test_empty_decision_list_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TriageOutput.model_validate({"decisions": []})


def test_empty_reasoning_is_rejected() -> None:
    """`reasoning` is posted verbatim to GitHub; blank is not an answer."""
    with pytest.raises(ValidationError):
        TriageOutput.model_validate(_payload(reasoning=""))
