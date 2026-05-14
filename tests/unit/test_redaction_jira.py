"""Redaction additions for feature 002 — Atlassian token + literal-string registry."""

from __future__ import annotations

import pytest

from daeyeon_bot.infra.logging import (
    REDACTED,
    clear_literal_secrets_for_testing,
    redact_text,
    redact_with_provenance,
    register_literal_secret,
)


@pytest.fixture(autouse=True)
def _reset_literals() -> None:  # pyright: ignore[reportUnusedFunction]
    """Autouse cleanup — pytest discovers it via collection, pyright doesn't."""
    clear_literal_secrets_for_testing()


def test_atlassian_token_pattern_scrubbed() -> None:
    """ATATT-prefixed Jira API tokens are scrubbed regardless of context."""
    # Real Atlassian tokens are ~190 chars after ATATT. Synthetic 40-char body
    # is the minimum the pattern matches.
    token = "ATATT" + "3xFfGF0_body-1234567890abcdefghijklmnopqrstuvwxyz"
    text = f"Authorization: Basic dXNlcjp{token}"
    out = redact_text(text)
    assert REDACTED in out
    assert token not in out


def test_atlassian_token_pattern_requires_minimum_body() -> None:
    """Short `ATATT...` strings don't match — avoids false-positive on normal text."""
    # 4 chars of body — below the {40,} cap.
    text = "see ATATTabcd done"
    out = redact_text(text)
    assert "ATATTabcd" in out


def test_register_literal_secret_redacts_dictionary_word() -> None:
    """`automation` as the SSW SSH password — too low entropy for the generic fallback,
    so we register the literal value at boot."""
    register_literal_secret("automation")
    text = "ssh automation@ssw-giga-02 with password automation"
    out = redact_text(text)
    # Both occurrences scrubbed
    assert out.count(REDACTED) == 2
    assert "automation" not in out


def test_register_literal_secret_respects_word_boundary() -> None:
    """`automation` should not redact `automation_password` or similar — that would
    over-scrub. Word-boundary anchors keep us conservative."""
    register_literal_secret("automation")
    text = "the automation_password field is set"
    out = redact_text(text)
    # `automation_password` contains `_` which is in the boundary class → no match.
    assert "automation_password" in out


def test_register_literal_secret_ignores_short_values() -> None:
    """Values < 3 chars are dropped silently — they'd scrub far too aggressively."""
    register_literal_secret("ab")
    text = "ab cd"
    assert redact_text(text) == text


def test_literal_redaction_reported_in_provenance() -> None:
    register_literal_secret("automation")
    _, spans = redact_with_provenance("user automation here")
    assert any(reason == "literal" for _, _, reason in spans)


def test_existing_patterns_still_work() -> None:
    """Sanity check: adding literal/atlassian patterns didn't break existing ones."""
    text = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa and AKIAIOSFODNN7EXAMPLE"
    out = redact_text(text)
    assert out.count(REDACTED) == 2
    assert "ghp_" not in out
    assert "AKIA" not in out
