"""Polling trigger for CI-failure alerts in the SSW DevOps on-call Slack channels.

Feature 003 P2. Mirrors `jira_assigned` / `gh_review_requested` shape, but a Slack
channel is a *stream*, so state is a per-channel high-water cursor
(`slack_ci_alert_state`) rather than a per-entity membership flag. Each poll, per
channel, applies the 5-case state machine (plan §Trigger state machine):

  CASE 1   row is NULL                 → cold-start: seed cursor to latest ts,
                                          emit nothing (no retroactive triage).
  CASE 1b  gap > staleness_seconds     → re-seed cursor to latest, emit nothing
                                          (don't back-fill stale runs after an outage).
  CASE 2   new CI-failure candidates   → emit one event per candidate (up to
                                          max_per_cycle), advancing the cursor
                                          per candidate in its own transaction.
  CASE 3   new messages, none CI       → advance cursor past the chatter, no emit.
  CASE 4   no new messages             → touch updated_at, no emit.
  CASE 5   PAUSE active                → skip the read entirely (no API call, no
                                          cursor move, no emit).

Errors: AuthError → re-raise (halts daemon); RateLimit/Transient/Permanent →
log + continue (Permanent also reported to the supervisor for quarantine).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiosqlite
import structlog

from daeyeon_bot.core.errors import (
    AuthError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from daeyeon_bot.core.events import make_event
from daeyeon_bot.core.manifest import TriggerManifest
from daeyeon_bot.core.protocols import EmitFn, TriggerContext
from daeyeon_bot.core.time import Clock
from daeyeon_bot.infra import outbox, slack_ci_alert_state
from daeyeon_bot.infra.alert_parse import is_ci_failure_candidate, merge_message_text

_log = structlog.get_logger(__name__)

_HANDLER_NAME = "ci_triage"
_SOURCE = "slack_ci_alert"
_EVENT_TYPE = "slack.ci_alert"

# Known alert-bot author ids (verified live 2026-06-19). sukju-bot has no stable
# id here but its posts always carry an actions/runs link, so the link branch of
# the candidate filter catches it.
_DEFAULT_KNOWN_BOT_IDS = frozenset({"U069J27G2G6", "U09RJGLLPLZ"})  # dev_syssw_test, SSW-Alert-Bot
_MAX_PAGES = 10  # safety cap on history pagination per channel per cycle

MANIFEST = TriggerManifest(
    name="slack_ci_alert",
    source=_SOURCE,
    retryable_at_source=False,
)

StorageFactory = Callable[[], AbstractAsyncContextManager[aiosqlite.Connection]]
PermanentFailureReporter = Callable[[str], Awaitable[bool]]


def _never_paused() -> bool:
    return False


@dataclass(frozen=True, slots=True)
class _PollOutcome:
    emitted: int = 0
    seeded: int = 0
    reseeded: int = 0


@dataclass(slots=True)
class SlackCiAlertTrigger:
    """Long-running poller over the on-call Slack channels."""

    slack: Any
    storage_factory: StorageFactory
    channels: tuple[str, ...]
    poll_interval_seconds: float
    max_per_cycle: int
    staleness_seconds: float
    clock: Clock
    manifest: TriggerManifest = MANIFEST
    pause_check: Callable[[], bool] = _never_paused
    permanent_failure_reporter: PermanentFailureReporter | None = None
    known_bot_ids: frozenset[str] = field(default=_DEFAULT_KNOWN_BOT_IDS)

    async def run(self, emit: EmitFn, ctx: TriggerContext) -> None:
        """Loop until cancelled. AuthError propagates and halts the daemon."""
        del emit, ctx  # the trigger persists events directly via storage_factory.
        while True:
            if self.pause_check():
                _log.info("slack_ci_alert.paused")
                await asyncio.sleep(self.poll_interval_seconds)
                continue
            try:
                outcome = await self.poll_once()
            except AuthError:
                raise
            except RateLimitError as exc:
                _log.warning("slack_ci_alert.rate_limited", error=str(exc))
            except TransientError as exc:
                _log.warning("slack_ci_alert.poll_failed", error=str(exc))
            except PermanentError as exc:
                _log.warning("slack_ci_alert.poll_failed", error=str(exc))
                if (
                    self.permanent_failure_reporter is not None
                    and await self.permanent_failure_reporter(str(exc))
                ):
                    _log.error("slack_ci_alert.quarantined", error=str(exc))
                    return
            else:
                _log.info(
                    "slack_ci_alert.poll_ok",
                    emitted=outcome.emitted,
                    seeded=outcome.seeded,
                    reseeded=outcome.reseeded,
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def poll_once(self) -> _PollOutcome:
        """One observe-and-emit pass across all channels."""
        emitted = seeded = reseeded = 0
        for channel_id in self.channels:
            result = await self._poll_channel(channel_id)
            emitted += result.emitted
            seeded += result.seeded
            reseeded += result.reseeded
        return _PollOutcome(emitted=emitted, seeded=seeded, reseeded=reseeded)

    async def _poll_channel(self, channel_id: str) -> _PollOutcome:
        now = self.clock.now()
        now_iso = now.isoformat()

        async with self.storage_factory() as conn:
            cursor = await slack_ci_alert_state.get_cursor(conn, channel_id)

            # CASE 1 — cold start.
            if cursor is None:
                latest = await self._latest_ts(channel_id)
                if latest is None:
                    _log.info("slack_ci_alert.cold_start_empty", channel=channel_id)
                    return _PollOutcome()
                await slack_ci_alert_state.seed_cursor(
                    conn, channel_id=channel_id, last_seen_ts=latest, now_iso=now_iso
                )
                await conn.commit()
                return _PollOutcome(seeded=1)

            # CASE 1b — staleness re-seed only after a real polling gap (a daemon
            # outage), measured from the LAST POLL (updated_at), NOT the last
            # message ts. A quiet but actively-polled channel must not age into a
            # reseed and silently eat its next alert. On an unparseable timestamp
            # we fail safe (treat as fresh → process normally, never eat alerts).
            last_poll = _iso_epoch(cursor.updated_at)
            if last_poll is not None and now.timestamp() - last_poll > self.staleness_seconds:
                latest = await self._latest_ts(channel_id)
                if latest is not None:
                    await slack_ci_alert_state.advance_cursor(
                        conn, channel_id=channel_id, last_seen_ts=latest, now_iso=now_iso
                    )
                    await conn.commit()
                    _log.warning("slack_ci_alert.stale_cursor_reseed", channel=channel_id)
                    return _PollOutcome(reseeded=1)

            # Fetch everything newer than the cursor (oldest→newest).
            messages = await self._fetch_since(channel_id, cursor.last_seen_ts)
            if not messages:
                await slack_ci_alert_state.touch(conn, channel_id=channel_id, now_iso=now_iso)
                await conn.commit()
                return _PollOutcome()

            newest_ts = messages[-1]["ts"]
            # Thread-aware candidacy: a candidate is a top-level message that is
            # itself a CI-failure alert, OR one whose thread replies carry the run
            # link (humans post "premerge fail" then drop the run URL in a reply).
            # Each candidate keeps the raw_blob that actually carries the evidence.
            candidates: list[tuple[dict[str, Any], str]] = []
            for msg in messages:
                raw_blob, is_candidate = await self._thread_candidacy(channel_id, msg)
                if is_candidate:
                    candidates.append((msg, raw_blob))

            # CASE 3 — new messages but no candidates: advance past the chatter.
            if not candidates:
                await slack_ci_alert_state.advance_cursor(
                    conn, channel_id=channel_id, last_seen_ts=newest_ts, now_iso=now_iso
                )
                await conn.commit()
                return _PollOutcome()

            # CASE 2 — emit per candidate (capped), advancing the cursor per
            # candidate in its own transaction so a mid-cycle crash leaves the
            # unemitted remainder re-readable with no skip / no double-emit.
            emit_list = candidates[: self.max_per_cycle]
            capped = len(candidates) > self.max_per_cycle
            emitted = 0
            for msg, raw_blob in emit_list:
                if await self._emit_event(
                    conn, channel_id=channel_id, msg=msg, raw_blob=raw_blob, now=now
                ):
                    emitted += 1
                await slack_ci_alert_state.advance_cursor(
                    conn, channel_id=channel_id, last_seen_ts=msg["ts"], now_iso=now_iso
                )
                await conn.commit()

            if capped:
                _log.warning(
                    "slack_ci_alert.max_per_cycle_hit",
                    channel=channel_id,
                    cap=self.max_per_cycle,
                    collected=len(candidates),
                )
                # cursor stopped at the last EMITTED candidate; remainder next cycle.
            else:
                # All candidates emitted — consume trailing chatter up to newest.
                await slack_ci_alert_state.advance_cursor(
                    conn, channel_id=channel_id, last_seen_ts=newest_ts, now_iso=now_iso
                )
                await conn.commit()
            return _PollOutcome(emitted=emitted)

    async def _thread_candidacy(
        self, channel_id: str, msg: dict[str, Any]
    ) -> tuple[str, bool]:
        """Return (raw_blob, is_candidate) for a top-level message, looking into
        its thread replies when the message itself isn't an alert. The run link is
        often in a reply ("premerge fail" parent + run URL reply); we merge the
        replies into raw_blob so the handler can extract the run from the parent's
        thread. Only threads (`reply_count > 0`) trigger the extra fetch."""
        parent_blob = merge_message_text(msg)
        if is_ci_failure_candidate(msg, known_bot_ids=self.known_bot_ids):
            return parent_blob, True
        if int(msg.get("reply_count", 0) or 0) <= 0:
            return parent_blob, False

        parent_ts = str(msg["ts"])
        thread = await self.slack.replies(channel_id, thread_ts=parent_ts)
        replies = [r for r in thread if str(r.get("ts")) != parent_ts]
        if not any(is_ci_failure_candidate(r, known_bot_ids=self.known_bot_ids) for r in replies):
            return parent_blob, False

        reply_blob = "\n".join(merge_message_text(r) for r in replies)
        combined = f"{parent_blob}\n{reply_blob}" if parent_blob else reply_blob
        return combined, True

    async def _latest_ts(self, channel_id: str) -> str | None:
        page = await self.slack.history(channel_id, limit=1)
        if not page.messages:
            return None
        ts = page.messages[0].get("ts")
        return str(ts) if ts is not None else None

    async def _fetch_since(self, channel_id: str, oldest: str) -> list[dict[str, Any]]:
        """All messages with ts > oldest, sorted ascending. Pages until exhausted
        (bounded by `oldest`, so the total is just the new messages)."""
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = await self.slack.history(channel_id, oldest=oldest, cursor=cursor, limit=100)
            collected.extend(m for m in page.messages if "ts" in m)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        collected.sort(key=lambda m: _ts_seconds(str(m["ts"])))
        return collected

    async def _emit_event(
        self,
        conn: aiosqlite.Connection,
        *,
        channel_id: str,
        msg: dict[str, Any],
        raw_blob: str,
        now: Any,
    ) -> bool:
        message_ts = str(msg["ts"])
        author = msg.get("user")
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "message_ts": message_ts,
            "author_id": author if isinstance(author, str) else None,
            "raw_blob": raw_blob,
        }
        seed = f"slack-ci-alert|{channel_id}|{message_ts}"
        dedup_key = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        event = make_event(type=_EVENT_TYPE, payload=payload, created_at=now)
        inserted = await outbox.insert_event(
            conn, event, source=_SOURCE, source_dedup_key=dedup_key
        )
        if not inserted:
            return False
        await outbox.enqueue_handler(conn, event_id=event.id, handler=_HANDLER_NAME, now=now)
        return True


def _ts_seconds(slack_ts: str) -> float:
    """Slack ts ('1718800000.001200') → float seconds. 0.0 on a malformed value."""
    try:
        return float(slack_ts)
    except (TypeError, ValueError):
        return 0.0


def _iso_epoch(iso: str) -> float | None:
    """ISO-8601 (`updated_at`) → epoch seconds, or None if unparseable."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


__all__ = [
    "MANIFEST",
    "PermanentFailureReporter",
    "SlackCiAlertTrigger",
    "StorageFactory",
]
