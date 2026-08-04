"""Extract a JSON object out of a free-form LLM text response.

Handlers ask Claude for "ONLY a JSON object, no prose, no code fence" and
then `json.loads` the reply. The model does not always comply: observed
replies wrap the object in a ```json fence (see `pr_review` capture
2026-08-04) and sometimes prepend a lead-in sentence.

Each handler previously carried its own `_strip_code_fence`, which removed a
fence only when it sat at offset 0:

    if stripped.startswith("```"): ...   # else: return the text untouched

So a single leading character sent the whole reply into `json.loads`, which
fails with `Expecting value: line 1 column 1 (char 0)`. That was the dominant
dead-letter cause across `pr_review` (396), `ci_triage` (44) and
`jira_triage` — and it was diagnosable only by its *absence* of the
`Extra data:` variant, which proved the noise was leading, not trailing.

`extract_json_object` scans for the first `{` that opens a brace-balanced,
*parseable* object, so leading prose, trailing prose, fences, and nested
objects are all tolerated. Brace counting is string-aware: braces inside JSON
string values (review bodies routinely embed code) must not shift the depth.
"""

from __future__ import annotations

import json

__all__ = ["extract_json_object"]

# Backstop for adversarial input only. The real object starts at one of the
# first few `{`; without a cap, a reply with thousands of braces and no valid
# object would make the scan quadratic.
_MAX_CANDIDATE_STARTS = 32


def extract_json_object(text: str) -> str:
    """Return the substring of `text` holding its first parseable JSON object.

    Falls back to the first brace-balanced slice, and finally to the stripped
    input, so the caller's `json.loads` still raises an error that describes
    the actual payload rather than one this function invented.
    """
    stripped = text.strip()
    if not stripped:
        return stripped

    first_balanced: str | None = None
    attempts = 0
    for start, char in enumerate(stripped):
        if char != "{":
            continue
        attempts += 1
        if attempts > _MAX_CANDIDATE_STARTS:
            break
        candidate = _balanced_slice(stripped, start)
        if candidate is None:
            continue
        if first_balanced is None:
            first_balanced = candidate
        try:
            json.loads(candidate)
        except ValueError:
            continue
        return candidate

    if first_balanced is not None:
        return first_balanced
    return stripped


def _balanced_slice(text: str, start: int) -> str | None:
    """Slice from `start` to its matching `}`, or None when never balanced.

    Tracks string/escape state: a `{` inside a JSON string is data, not depth.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
