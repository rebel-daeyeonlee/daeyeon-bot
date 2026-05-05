"""System-prompt assembly for the `pr_review` handler.

Split out of `pr_review.py` so prompt-engineering changes live in a small
file with no business logic. The directive below pins the JSON schema, the
sign-off marker, and the inline-vs-summary split that downstream code
(`_append_folded_bullets`, `_filter_anchors`) depends on.
"""

from __future__ import annotations

import json

from daeyeon_bot.handlers.pr_review_schemas import ReviewOutput

# Output directive appended to every persona body — anchors length,
# language, sign-off, and the inline-vs-summary split. The persona is a
# chat-mode markdown reviewer; without this directive the model emits
# prose and the handler's `json.loads` fails with
# "Expecting value: line 1 column 1 (char 0)".
OUTPUT_DIRECTIVE = (
    "\n\n---\n\n"
    "You are reviewing the pull request below as the daeyeon-bot PR-bound"
    " caller. Output ONLY a JSON object that matches this exact JSON schema."
    " No prose before or after, no Markdown code fence — just the JSON object"
    " on stdout. If you have nothing to flag, emit an empty `comments` array"
    " but still produce a meaningful `summary`.\n\n"
    "`summary` rules (HARD — validation fails otherwise):\n"
    "- Target <= 1500 chars, hard cap 2500 chars.\n"
    "- Body language: Korean (한국어). Keep ASCII for severity labels"
    " (CRITICAL / MAJOR / MINOR), verdict labels (PASS / CONCERNS / FAIL),"
    " rule IDs ([G35], [P1], ...), file:line anchors, code identifiers,"
    " and the sign-off marker.\n"
    "- Section order (top to bottom): optional `**Reviewer**: as Senior"
    " <Role>` line (only when role-primed; omit otherwise), then"
    " `**Verdict**: <PASS | CONCERNS | FAIL> — <한 문장 근거>` line,"
    " then 개요 (2-3 Korean sentences plain walkthrough),"
    " Findings table — flat table when N findings <= 6, wrapped in"
    " `<details open><summary>...</summary> ... </details>` when N > 6"
    " (default-expanded so reviewers see findings without clicking),"
    " omitted entirely when N == 0; then Positive (0-2 Korean bullets,"
    " omit section if empty),"
    " sign-off.\n"
    "- NO Detail prose, NO `### N. [SEV]` sub-sections, NO code fences in summary.\n"
    "- Findings table 설명 cell <= 80 chars, single line, no newlines /"
    " fences / multi-sentence prose. Evidence + fix go to inline comments.\n"
    "- Sign-off (REQUIRED): the very last non-empty line MUST be exactly"
    " `— daeyeon-bot 🐥` (or `— daeyeon-bot 🐥 (as Senior <Role>)` when"
    " role-primed). Exactly one blank line before it.\n\n"
    "`comments[]` rules (HARD):\n"
    "- Every CRITICAL and MAJOR finding MUST appear as one InlineComment"
    " anchored to its file:line, carrying Korean evidence + suggested fix"
    " (multi-line + code fence OK).\n"
    "- MINOR inline is optional.\n"
    "- Inline body first line: `[SEVERITY] file:line — 한 문장.` (period included).\n"
    "- Inline body MUST NOT contain the sign-off marker"
    " (`— daeyeon-bot 🐥`). Sign-off belongs to the summary only;"
    " repeating it on every inline pollutes the PR.\n\n"
    "JSON schema:\n"
)


def build_system_prompt(persona_body: str) -> str:
    schema_dump = json.dumps(ReviewOutput.model_json_schema(), indent=2)
    return persona_body + OUTPUT_DIRECTIVE + schema_dump


__all__ = ["OUTPUT_DIRECTIVE", "build_system_prompt"]
