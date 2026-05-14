"""structlog wiring + secret-redaction processor.

Redaction is mandatory before any handler logs Claude payloads. We match
known token shapes (Slack, AWS, JWT, Anthropic OAuth, GitHub) plus a
high-entropy fallback so unknown-shape secrets don't slip through.

The redactor walks the structlog event_dict recursively (dicts, lists,
strings) and replaces matches with `***REDACTED***`.
"""

from __future__ import annotations

import logging
import math
import re
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, cast

import structlog

REDACTED = "***REDACTED***"

RedactReason = str
# one of: "slack" | "aws" | "jwt" | "anthropic" | "gh" | "atlassian" | "literal" | "entropy"

# Order matters only for performance; matches are replaced wherever they appear.
_NAMED_PATTERNS: tuple[tuple[RedactReason, re.Pattern[str]], ...] = (
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("anthropic", re.compile(r"sk-ant-oat[A-Za-z0-9_-]{10,}")),
    ("anthropic", re.compile(r"sk-ant-api[0-9]{2}-[A-Za-z0-9_-]{10,}")),
    ("gh", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("gh", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    # Atlassian Cloud API tokens — feature 002 (`JIRA_API_TOKEN`).
    # Documented prefix `ATATT` followed by base64url body.
    ("atlassian", re.compile(r"\bATATT[A-Za-z0-9_-]{40,}\b")),
)
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(p for _, p in _NAMED_PATTERNS)

# Literal-string patterns registered at boot via `register_literal_secret()`.
# Used for secrets whose shape is too weak for a generic regex
# (e.g. `SSW_AUTOMATION_PASSWORD="automation"` — a 10-char dictionary word
# that the entropy fallback won't catch).
_LITERAL_REDACTIONS: list[re.Pattern[str]] = []


def register_literal_secret(value: str) -> None:
    """Register a literal string value to scrub from every log line.

    Called once at boot per loaded named secret. Pass the secret value as
    a plain string; we compile a word-boundary-anchored regex so we don't
    over-redact innocent substrings.
    """
    if not value or len(value) < 3:
        # Below 3 chars is too noisy — would scrub common words. Operator
        # should pick a longer secret if they want literal redaction.
        return
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])")
    _LITERAL_REDACTIONS.append(pattern)


def clear_literal_secrets_for_testing() -> None:
    """Test helper: clear the registered-literal list. Tests only."""
    _LITERAL_REDACTIONS.clear()


# Entropy fallback parameters.
_MIN_ENTROPY_BITS_PER_CHAR = 4.5
_MIN_TOKEN_LEN = 24
_TOKENISH_RE = re.compile(r"[A-Za-z0-9_-]{24,}")


def init(*, level: str = "INFO", fmt: str = "console") -> None:
    """Initialize structlog with a redaction processor."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stderr, level=log_level, format="%(message)s")

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )


def redact_processor(
    _logger: object, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: returns a new event_dict with secrets redacted."""
    return cast("MutableMapping[str, Any]", _redact_value(event_dict))


def redact_text(text: str) -> str:
    """Redact known-shape, literal, and high-entropy tokens in `text`."""
    out = text
    for pattern in _LITERAL_REDACTIONS:
        out = pattern.sub(REDACTED, out)
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = _TOKENISH_RE.sub(_maybe_redact_match, out)
    return out


def redact_with_provenance(text: str) -> tuple[str, list[tuple[int, int, RedactReason]]]:
    """Same redaction as `redact_text`, plus the spans + reasons for each match.

    Spans use the *original* `text` offsets (not the redacted output's), so
    callers can correlate flags back to the model output without re-running
    a regex. The redacted text is the same as `redact_text(text)`.
    """
    spans: list[tuple[int, int, RedactReason]] = []
    for reason, pattern in _NAMED_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end(), reason))
    for pattern in _LITERAL_REDACTIONS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end(), "literal"))
    for m in _TOKENISH_RE.finditer(text):
        if _shannon_entropy(m.group(0)) >= _MIN_ENTROPY_BITS_PER_CHAR:
            # Skip if already covered by a named match — named wins on overlap.
            if any(start <= m.start() and m.end() <= end for start, end, _ in spans):
                continue
            spans.append((m.start(), m.end(), "entropy"))
    spans.sort()
    return redact_text(text), spans


def _maybe_redact_match(match: re.Match[str]) -> str:
    candidate = match.group(0)
    if _shannon_entropy(candidate) >= _MIN_ENTROPY_BITS_PER_CHAR:
        return REDACTED
    return candidate


def _shannon_entropy(s: str) -> float:
    """Bits/character via Shannon entropy. Empty / single-char strings → 0.0."""
    if len(s) < _MIN_TOKEN_LEN:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {key: _redact_value(v) for key, v in mapping.items()}
    if isinstance(value, tuple):
        items = cast("tuple[object, ...]", value)
        return tuple(_redact_value(v) for v in items)
    if isinstance(value, list):
        items_list = cast("list[object]", value)
        return [_redact_value(v) for v in items_list]
    if isinstance(value, Iterable) and not isinstance(value, bytes | bytearray):
        iterable = cast("Iterable[object]", value)
        return [_redact_value(v) for v in iterable]  # pragma: no cover — defensive
    return value


__all__ = [
    "REDACTED",
    "RedactReason",
    "clear_literal_secrets_for_testing",
    "init",
    "redact_processor",
    "redact_text",
    "redact_with_provenance",
    "register_literal_secret",
]
