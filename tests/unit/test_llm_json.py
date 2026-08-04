"""`core.llm_json.extract_json_object` — LLM reply → JSON substring.

The regression these cover: the previous per-handler `_strip_code_fence`
un-fenced only at offset 0, so a single leading character sent the whole
reply into `json.loads` and produced
`Expecting value: line 1 column 1 (char 0)` — 440 dead letters across
pr_review / ci_triage / jira_triage between 2026-07-24 and 2026-08-04.
"""

from __future__ import annotations

import json

import pytest

from daeyeon_bot.core.llm_json import extract_json_object

FENCE = "```"


# ── shapes the old helper already handled (must not regress) ──────────────


def test_bare_object_passthrough() -> None:
    assert extract_json_object('{"x": 1}') == '{"x": 1}'


def test_fence_with_lang_tag() -> None:
    assert extract_json_object(f'{FENCE}json\n{{"x": 1}}\n{FENCE}') == '{"x": 1}'


def test_fence_without_lang_tag() -> None:
    assert extract_json_object(f'{FENCE}\n{{"x": 1}}\n{FENCE}') == '{"x": 1}'


def test_unclosed_opening_fence() -> None:
    assert extract_json_object(f'{FENCE}json\n{{"x": 1}}') == '{"x": 1}'


def test_surrounding_whitespace() -> None:
    assert extract_json_object('\n\n  {"x": 1}  \n\n') == '{"x": 1}'


# ── the shapes that were dead-lettering ───────────────────────────────────


def test_leading_prose_then_fence() -> None:
    """The production failure: lead-in sentence ahead of a fenced object."""
    raw = f'리뷰 결과입니다:\n\n{FENCE}json\n{{"x": 1}}\n{FENCE}'
    assert extract_json_object(raw) == '{"x": 1}'


def test_leading_prose_then_bare_object() -> None:
    assert extract_json_object('Here is the review:\n{"x": 1}') == '{"x": 1}'


def test_trailing_prose_after_fence() -> None:
    """Old helper produced `Extra data:` here; now the tail is dropped."""
    raw = f'{FENCE}json\n{{"x": 1}}\n{FENCE}\n\n이상입니다.'
    assert extract_json_object(raw) == '{"x": 1}'


def test_prose_on_both_sides() -> None:
    raw = f'분석했습니다.\n\n{FENCE}json\n{{"x": 1}}\n{FENCE}\n\n추가 질문 있으면 알려주세요.'
    assert extract_json_object(raw) == '{"x": 1}'


# ── brace counting must be string-aware ───────────────────────────────────


def test_braces_inside_string_values_do_not_shift_depth() -> None:
    """Review bodies embed code; `{` in a string value is data, not depth."""
    raw = '{"body": "use {code} here", "n": 1}'
    assert json.loads(extract_json_object(raw)) == {"body": "use {code} here", "n": 1}


def test_escaped_quote_inside_string() -> None:
    raw = '{"body": "he said \\"hi\\" then {", "n": 1}'
    assert json.loads(extract_json_object(raw))["n"] == 1


def test_nested_objects_return_outermost() -> None:
    raw = '{"outer": {"inner": {"deep": 1}}}'
    assert extract_json_object(raw) == raw


def test_prose_containing_a_brace_before_the_real_object() -> None:
    """A `{` in the prose must not anchor extraction to invalid JSON."""
    raw = 'note: the { char appears in prose\n{"x": 1}'
    assert extract_json_object(raw) == '{"x": 1}'


def test_real_capture_shape_round_trips() -> None:
    """Mirrors the 2026-08-04 capture: fenced object with escaped newlines."""
    obj = {
        "verdict": "PASS",
        "summary": "**Verdict**: PASS — 근거\n\n| # | SEV |\n|---|---|\n\n— daeyeon-bot 🐥",
        "comments": [{"path": "inv/submodule.py", "line": 1373, "body": '[MINOR] `("","")` 경로'}],
    }
    raw = f"{FENCE}json\n{json.dumps(obj, ensure_ascii=False)}\n{FENCE}"
    assert json.loads(extract_json_object(raw)) == obj


# ── degenerate input: stay honest, let the caller's error describe reality ─


@pytest.mark.parametrize("raw", ["", "   \n  "])
def test_empty_input_returns_empty(raw: str) -> None:
    assert extract_json_object(raw) == ""


def test_no_object_at_all_returns_stripped_input() -> None:
    """No `{` anywhere → caller still reports the payload it actually got."""
    assert extract_json_object("  I cannot review this PR.  ") == "I cannot review this PR."


def test_unbalanced_object_is_returned_for_the_caller_to_reject() -> None:
    raw = '{"x": 1'
    assert extract_json_object(raw) == raw
    with pytest.raises(ValueError, match="Expecting"):
        json.loads(extract_json_object(raw))
