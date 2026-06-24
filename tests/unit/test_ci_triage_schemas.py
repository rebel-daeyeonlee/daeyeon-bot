"""TriageOutput validation + confidence floor (feature 003)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from daeyeon_bot.handlers.ci_triage_schemas import (
    Evidence,
    TriageOutput,
    WikiRef,
    enforce_confidence_floor,
)

_BASE = {
    "classification": "environment",
    "owner_area": "DevOps",
    "headline": "QEMU golden-base missing",
    "summary": "golden base image missing",
    "likely_cause": "qemu golden base image deleted from NFS",
    "recommended_action": "rebuild golden base image",
    "rerun_advice": "needs_investigation",
    "needs_human": True,
}


def test_valid_output_round_trips() -> None:
    out = TriageOutput(
        attribution="infra_env",
        confidence="medium",
        log_evidence=(
            Evidence(quote="rsync ... golden-base failed", citation="premerge / result"),
        ),
        wiki_matches=(
            WikiRef(
                path="wiki/oncall/incidents/qemu-golden-base-image-missing.md", why="signature"
            ),
        ),
        **_BASE,  # type: ignore[arg-type]
    )
    assert out.attribution == "infra_env"


def test_evidence_required_when_attribution_not_unknown() -> None:
    with pytest.raises(PydanticValidationError, match="log_evidence is required"):
        TriageOutput(attribution="infra_env", confidence="low", **_BASE)  # type: ignore[arg-type]


def test_unknown_attribution_allows_no_evidence() -> None:
    out = TriageOutput(attribution="unknown", confidence="low", **_BASE)  # type: ignore[arg-type]
    assert out.log_evidence == ()


def test_high_confidence_requires_grounding() -> None:
    with pytest.raises(PydanticValidationError, match="confidence 'high' requires"):
        TriageOutput(attribution="unknown", confidence="high", **_BASE)  # type: ignore[arg-type]


def test_confidence_floor_caps_to_low_without_anchor_or_wiki() -> None:
    out = TriageOutput(
        attribution="unknown",
        confidence="medium",
        **_BASE,  # type: ignore[arg-type]
    )
    capped = enforce_confidence_floor(out, has_strong_anchor=False, has_wiki_match=False)
    assert capped.confidence == "low"


def test_confidence_floor_allows_medium_with_strong_anchor() -> None:
    out = TriageOutput(
        attribution="infra_env",
        confidence="medium",
        log_evidence=(Evidence(quote="VM creation failed", citation="result"),),
        **_BASE,  # type: ignore[arg-type]
    )
    capped = enforce_confidence_floor(out, has_strong_anchor=True, has_wiki_match=False)
    assert capped.confidence == "medium"


def test_confidence_floor_allows_medium_with_cross_run_signal() -> None:
    """P1: a decisive cross-run comparison alone (no wiki, no strong anchor) lifts
    the ceiling to medium."""
    out = TriageOutput(
        attribution="infra_env",
        confidence="medium",
        log_evidence=(Evidence(quote="other PRs also fail", citation="cross-run"),),
        **_BASE,  # type: ignore[arg-type]
    )
    capped = enforce_confidence_floor(
        out, has_strong_anchor=False, has_wiki_match=False, has_cross_run_signal=True
    )
    assert capped.confidence == "medium"


def test_owner_area_matches_pinned_vault_domain_vocab() -> None:
    """Drift guard: TriageOutput.owner_area Literal == the pinned ssw-devops-oncall
    `domain:` vocabulary. Reads the pinned list from the contract doc for
    hermeticity (does not hit the live vault)."""
    contract = Path(__file__).resolve().parents[2] / (
        "specs/003-ci-monitor-bot/contracts/oncall-wiki-surface.md"
    )
    if not contract.is_file():
        pytest.skip("contract doc not yet written (lands with P1 docs)")
    text = contract.read_text(encoding="utf-8")
    m = re.search(r"DOMAIN_VOCABULARY\s*=\s*\[([^\]]+)\]", text)
    assert m is not None, "pin DOMAIN_VOCABULARY in oncall-wiki-surface.md"
    pinned = {v.strip().strip("\"'") for v in m.group(1).split(",") if v.strip()}
    literal = {"DevOps", "SysFw", "SysSol", "Connectivity", "Driver", "HW"}
    assert literal == pinned
