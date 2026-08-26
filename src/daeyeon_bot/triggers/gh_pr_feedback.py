"""Polling trigger for unanswered review comments on the operator's own PRs.

Feature 004. Runs the same `author:<operator>` GitHub search the `pr_review`
`review_self` path uses, then asks a different question of each hit: not "was I
asked to review this?" but "did anyone leave feedback here that I have not
answered yet?".

The answer is a set difference, and that is the whole loop-termination story:

    pending = {live comment ids passing the author filter}
            - {ids already in the `pr_autofix_comment` ledger}

`pending` empty → nothing is emitted → the PR goes quiet. The operator's
"추가 지적사항이 없을 때까지" is not a countdown or a timeout; it is this set
draining as the handler answers each comment. A reviewer pushing back adds a
NEW comment id, which re-fills the set, and the loop resumes on its own.

The event's `source_dedup_key` hashes the pending id set, so
`events.UNIQUE(source, source_dedup_key)` makes a re-poll of an unchanged set a
no-op while one added comment produces a genuinely new event.

Cost per cycle in steady state is one `/search/issues` call: a PR whose
`updated_at` has not moved past our last observation is skipped without any
comment fetch. `force_rescan_seconds` puts a floor under that optimization so a
missed `updated_at` bump can never wedge a PR permanently.

Error mapping:
    AuthError      → re-raise (halts the daemon, exit 78)
    RateLimitError → skip the cycle
    other          → log + continue (next cycle retries)
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

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
from daeyeon_bot.handlers.pr_autofix_comments import build_feedback
from daeyeon_bot.infra import (
    outbox,
    pr_autofix_ledger,
    pr_feedback_state,
    pr_review_audit,
)
from daeyeon_bot.triggers.gh_review_requested import (
    PermanentFailureReporter,
    StorageFactory,
    parse_search_item,
)

_log = structlog.get_logger(__name__)

_HANDLER_NAME = "pr_autofix"
_SOURCE = "gh_pr_feedback"
_EVENT_TYPE = "gh.pr_feedback"

MANIFEST = TriggerManifest(
    name="gh_pr_feedback",
    source=_SOURCE,
    retryable_at_source=False,
)


def _never_paused() -> bool:
    return False


@dataclass(slots=True)
class GhPrFeedbackTrigger:
    """Long-running poller for unanswered review feedback on authored PRs."""

    gh: Any
    storage_factory: StorageFactory
    github_username: str
    poll_interval_seconds: float
    clock: Clock
    manifest: TriggerManifest = MANIFEST
    max_per_cycle: int = 5
    # Narrowing fragment for the GitHub search, built by
    # `gh_review_requested.build_search_extra_query` from `allowed_repos`.
    search_extra_query: str = ""
    # Mirrors `[handlers.pr_autofix]`. The trigger applies the same author
    # filter the handler does, so a PR whose only new comments are from an
    # ignored author never wakes the handler at all.
    comment_authors: list[str] = field(default_factory=lambda: ["*"])
    ignored_authors: list[str] = field(default_factory=list[str])
    self_comment_markers: list[str] = field(default_factory=lambda: ["daeyeon-bot autofix"])
    # Upper bound on how long the `updated_at` short-circuit may suppress a
    # comment fetch. GitHub does bump a PR's `updated_at` on new comments, but
    # this trigger's whole value depends on that being true, so we re-scan
    # every PR at least this often regardless.
    force_rescan_seconds: float = 1800.0
    pause_check: Callable[[], bool] = _never_paused
    permanent_failure_reporter: PermanentFailureReporter | None = None
    # Set of `(repo, pr_number)` last force-rescanned, with the timestamp.
    _last_full_scan: dict[tuple[str, int], float] = field(
        default_factory=dict[tuple[str, int], float], init=False
    )

    async def run(self, emit: EmitFn, ctx: TriggerContext) -> None:
        """Loop until cancelled. Polls first, then sleeps."""
        del emit, ctx  # events are persisted directly via storage_factory.
        while True:
            if self.pause_check():
                _log.info("gh_pr_feedback.paused")
                await asyncio.sleep(self.poll_interval_seconds)
                continue
            try:
                emitted = await self.poll_once()
            except AuthError:
                raise
            except RateLimitError:
                _log.warning("gh_pr_feedback.rate_limited")
            except TransientError as exc:
                _log.warning("gh_pr_feedback.poll_failed", error=str(exc))
            except PermanentError as exc:
                _log.warning("gh_pr_feedback.poll_failed", error=str(exc))
                if (
                    self.permanent_failure_reporter is not None
                    and await self.permanent_failure_reporter(str(exc))
                ):
                    _log.error("gh_pr_feedback.quarantined", error=str(exc))
                    return
            else:
                if emitted:
                    _log.info("gh_pr_feedback.emitted", count=emitted)
            await asyncio.sleep(self.poll_interval_seconds)

    async def poll_once(self) -> int:
        """One observe-and-emit pass. Returns the number of events emitted."""
        items = await self.gh.search_authored(
            self.github_username, extra_query=self.search_extra_query
        )
        item_updated_at: dict[tuple[str, int], str | None] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            pair = parse_search_item(cast("dict[str, Any]", raw))
            if pair is not None:
                updated = raw.get("updated_at")
                item_updated_at[pair] = updated if isinstance(updated, str) else None
        observed = set(item_updated_at)

        # Snapshot BEFORE any write. `mark_observed` stamps `last_observed_at`
        # with `now`, so comparing a freshly-written row against `updated_at`
        # would always look fresh and suppress every comment fetch forever.
        async with self.storage_factory() as conn:
            persisted = await pr_feedback_state.select_all(conn)

        now = self.clock.now()
        now_iso = now.astimezone(UTC).isoformat()
        monotonic = now.timestamp()

        emitted = 0
        for repo, pr_number in sorted(observed):
            if emitted >= self.max_per_cycle:
                _log.info(
                    "gh_pr_feedback.cycle_cap_reached",
                    cap=self.max_per_cycle,
                    remaining=len(observed) - emitted,
                )
                break
            state = persisted.get((repo, pr_number))
            if self._can_skip_scan(
                key=(repo, pr_number),
                state=state,
                item_updated_at=item_updated_at.get((repo, pr_number)),
                monotonic=monotonic,
            ):
                await self._touch(repo, pr_number, state, now_iso)
                continue
            self._last_full_scan[(repo, pr_number)] = monotonic
            if await self._scan_and_emit(repo, pr_number, now=now, now_iso=now_iso):
                emitted += 1

        await self._withdraw_missing(observed, persisted, now_iso)
        return emitted

    # ── per-PR work ───────────────────────────────────────────────────────

    def _can_skip_scan(
        self,
        *,
        key: tuple[str, int],
        state: pr_feedback_state.FeedbackStateRow | None,
        item_updated_at: str | None,
        monotonic: float,
    ) -> bool:
        """True when the PR provably has no new activity since our last look."""
        if state is None:
            return False
        last_full = self._last_full_scan.get(key)
        if last_full is None or monotonic - last_full >= self.force_rescan_seconds:
            return False
        state_dt = _iso_datetime(state.last_observed_at)
        item_dt = _iso_datetime(item_updated_at)
        if state_dt is None or item_dt is None:
            return False
        return state_dt >= item_dt

    async def _touch(
        self,
        repo: str,
        pr_number: int,
        state: pr_feedback_state.FeedbackStateRow | None,
        now_iso: str,
    ) -> None:
        """Refresh `last_observed_at` for a PR we skipped scanning."""
        head_sha = state.head_sha if state is not None else ""
        if not head_sha:
            return
        async with self.storage_factory() as conn:
            await pr_feedback_state.mark_observed(
                conn, repo=repo, pr_number=pr_number, head_sha=head_sha, now_iso=now_iso
            )
            await conn.commit()

    async def _scan_and_emit(
        self, repo: str, pr_number: int, *, now: datetime, now_iso: str
    ) -> bool:
        """Fetch comments for one PR and emit an event if anything is pending."""
        try:
            pr = await self.gh.pr_get(repo, pr_number)
        except (AuthError, RateLimitError):
            raise
        except (PermanentError, TransientError) as exc:
            _log.warning(
                "gh_pr_feedback.pr_get_failed", repo=repo, pr_number=pr_number, error=str(exc)
            )
            return False
        head_sha = _extract_head_sha(pr)
        if head_sha is None:
            return False

        try:
            review_comments = await self.gh.list_review_comments(repo, pr_number)
            reviews = await self.gh.list_reviews(repo, pr_number)
            issue_comments = await self.gh.list_issue_comments(repo, pr_number)
        except (AuthError, RateLimitError):
            raise
        except (PermanentError, TransientError) as exc:
            _log.warning(
                "gh_pr_feedback.comment_fetch_failed",
                repo=repo,
                pr_number=pr_number,
                error=str(exc),
            )
            return False

        async with self.storage_factory() as conn:
            own_review_ids = await pr_review_audit.posted_review_ids(conn, repo, pr_number)

        feedback = build_feedback(
            review_comments=review_comments,
            reviews=reviews,
            issue_comments=issue_comments,
            operator_login=self.github_username,
            allow_globs=self.comment_authors,
            ignore_globs=self.ignored_authors,
            self_comment_markers=self.self_comment_markers,
            own_review_ids=own_review_ids,
        )

        async with self.storage_factory() as conn:
            handled = await pr_autofix_ledger.handled_comment_ids(
                conn, repo=repo, pr_number=pr_number
            )
            pending = sorted(c.comment_id for c in feedback if c.comment_id not in handled)
            state = await pr_feedback_state.mark_observed(
                conn, repo=repo, pr_number=pr_number, head_sha=head_sha, now_iso=now_iso
            )
            if not pending:
                # The termination case, and by far the common one once a PR has
                # settled. Nothing to emit; the PR stays observed and quiet.
                await conn.commit()
                return False
            # Consume the round only if an event is actually written. Doing it
            # first burned a round on every re-poll of an UNCHANGED pending set:
            # the dedup key is a hash of that set, so `_emit_event` correctly
            # returns False, but the counter had already moved. A PR whose
            # comments stay pending on purpose — which is exactly what
            # `verify_failed` leaves behind — then walked its own round budget
            # to zero on polling frequency alone and stood itself down without
            # ever doing the work. Observed on ssw-bundle#5250: round 6, three
            # events.
            round_no = state.round + 1
            wrote = await _emit_event(
                conn,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                round_no=round_no,
                comment_ids=pending,
                now=now,
                now_iso=now_iso,
            )
            if wrote:
                round_no = await pr_feedback_state.consume_round(
                    conn, repo=repo, pr_number=pr_number, now_iso=now_iso
                )
            await conn.commit()
        if wrote:
            _log.info(
                "gh_pr_feedback.pending",
                repo=repo,
                pr_number=pr_number,
                pending=len(pending),
                round=round_no,
            )
        return wrote

    async def _withdraw_missing(
        self,
        observed: set[tuple[str, int]],
        persisted: dict[tuple[str, int], pr_feedback_state.FeedbackStateRow],
        now_iso: str,
    ) -> None:
        """Flip rows for PRs that left the search result (merged / closed)."""
        gone = [key for key, row in persisted.items() if row.in_pending_set and key not in observed]
        if not gone:
            return
        async with self.storage_factory() as conn:
            for repo, pr_number in gone:
                await pr_feedback_state.mark_withdrawn(
                    conn, repo=repo, pr_number=pr_number, now_iso=now_iso
                )
                self._last_full_scan.pop((repo, pr_number), None)
            await conn.commit()


async def _emit_event(
    conn: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    round_no: int,
    comment_ids: list[int],
    now: datetime,
    now_iso: str,
) -> bool:
    payload: dict[str, Any] = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "round": round_no,
        "comment_ids": comment_ids,
        "observed_at": now_iso,
    }
    # Hash the pending SET, not a counter: re-polling the same unanswered
    # comments must be a no-op, while one new comment must produce a new event.
    seed = f"gh-pr-feedback|{repo}#{pr_number}|" + ",".join(str(c) for c in comment_ids)
    dedup_key = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    event = make_event(type=_EVENT_TYPE, payload=payload, created_at=now)
    inserted = await outbox.insert_event(conn, event, source=_SOURCE, source_dedup_key=dedup_key)
    if not inserted:
        return False
    await outbox.enqueue_handler(conn, event_id=event.id, handler=_HANDLER_NAME, now=now)
    return True


def _iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_head_sha(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    head = payload.get("head")
    if not isinstance(head, dict):
        return None
    sha = head.get("sha")
    return sha if isinstance(sha, str) and sha else None


__all__ = ["MANIFEST", "GhPrFeedbackTrigger"]
