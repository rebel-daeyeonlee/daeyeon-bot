"""Feedback loop — reconcile Slack reactions on posted triages into accuracy
data (feature 003 D).

The bot posts a triage; the operator reacts ✅ (right) or ❌ (wrong). This module
reads those reactions for posted rows still missing feedback and records a
verdict on `ci_triage_audit`, so `inspect ci-triage stats` can report accuracy
and the confidence floor / persona can be tuned with data, not guesswork.

Pure classification (`classify_reactions`) is split from the I/O collector
(`collect_feedback`) so the mapping is unit-testable without Slack. Best-effort:
a per-message reaction fetch that fails is skipped, never fatal.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog

from daeyeon_bot.infra.ci_triage_audit import (
    Feedback,
    list_posted_awaiting_feedback,
    set_feedback,
)

_log = structlog.get_logger(__name__)

# Default reaction → verdict mapping. Kept small and unambiguous.
CORRECT_EMOJIS = frozenset({"white_check_mark", "heavy_check_mark", "+1", "100"})
INCORRECT_EMOJIS = frozenset({"x", "no_entry", "no_entry_sign", "-1", "thumbsdown"})


class _ReactionSource(Protocol):
    async def reactions_get(self, channel_id: str, timestamp: str) -> list[tuple[str, int]]: ...


def classify_reactions(
    names: Iterable[str],
    *,
    correct_emojis: frozenset[str] = CORRECT_EMOJIS,
    incorrect_emojis: frozenset[str] = INCORRECT_EMOJIS,
) -> tuple[Feedback, str] | None:
    """Map reaction emoji names to a verdict + the deciding emoji.

    Both ✅ and ❌ present → `unsure` (conflicting signal). Only ❌ → incorrect,
    only ✅ → correct, neither → None (nothing to record yet)."""
    name_set = set(names)
    pos = sorted(name_set & correct_emojis)
    neg = sorted(name_set & incorrect_emojis)
    if pos and neg:
        return ("unsure", f"{pos[0]}+{neg[0]}")
    if neg:
        return ("incorrect", neg[0])
    if pos:
        return ("correct", pos[0])
    return None


async def collect_feedback(
    conn: Any,  # aiosqlite.Connection
    slack: _ReactionSource,
    *,
    now: datetime,
    window_days: int = 14,
    limit: int = 100,
    correct_emojis: frozenset[str] = CORRECT_EMOJIS,
    incorrect_emojis: frozenset[str] = INCORRECT_EMOJIS,
) -> int:
    """Reconcile reactions for posted triages missing feedback in the window.
    Returns the number of rows updated. Per-row failures are logged and skipped."""
    since_iso = (now - timedelta(days=window_days)).isoformat()
    awaiting = await list_posted_awaiting_feedback(conn, since_iso=since_iso, limit=limit)
    updated = 0
    for row in awaiting:
        try:
            reactions = await slack.reactions_get(row.channel_id, row.message_ts)
        except Exception as exc:
            msg = str(exc)
            # A token-wide failure (no reactions:read scope, bad auth) won't fix
            # itself mid-pass — stop now instead of hammering Slack once per row.
            if any(s in msg for s in ("missing_scope", "not_authed", "invalid_auth")):
                _log.warning("ci_feedback.disabled", reason=msg)
                break
            _log.info("ci_feedback.reactions_failed", audit_id=row.audit_id, error=repr(exc))
            continue
        verdict = classify_reactions(
            (name for name, _count in reactions),
            correct_emojis=correct_emojis,
            incorrect_emojis=incorrect_emojis,
        )
        if verdict is None:
            continue
        feedback, emoji = verdict
        await set_feedback(
            conn,
            audit_id=row.audit_id,
            feedback=feedback,
            emoji=emoji,
            at_iso=now.isoformat(),
        )
        await conn.commit()
        updated += 1
    if updated:
        _log.info("ci_feedback.collected", updated=updated, scanned=len(awaiting))
    return updated


__all__ = [
    "CORRECT_EMOJIS",
    "INCORRECT_EMOJIS",
    "classify_reactions",
    "collect_feedback",
]
