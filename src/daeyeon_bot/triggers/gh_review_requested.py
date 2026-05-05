"""Polling trigger for `review-requested:<operator>` searches on GitHub.

The trigger sleeps `poll_interval_seconds` between observations, calls
`gh.search_review_requested`, applies the §5 case table from
`data-model.md` to the union of "PRs returned now" and "PRs we have a
state row for", and emits `gh.review_requested` events for cases (1, 2, 3)
in the same SQLite transaction as the state UPSERT — so a crash between
INSERT and UPSERT can't drop an event nor double-fire one.

Events fan out into the outbox keyed by
`source_dedup_key = sha256("gh-review-requested|{repo}#{pr}@{sha}#{gen}")`.
The `events.UNIQUE(source, source_dedup_key)` constraint makes a polled
re-emit of the *same* `(head_sha, gen)` a no-op — even if two pollers
ever raced (they cannot in this single-process daemon).

Error mapping:
    AuthError      → re-raise (halts the daemon, exit 78)
    RateLimitError → skip the cycle, sleep one extra interval
    other transient/permanent → log + continue (next cycle retries)
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
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
from daeyeon_bot.infra import outbox
from daeyeon_bot.infra.pr_review_state import StateRow, upsert_observation

_log = structlog.get_logger(__name__)

_HANDLER_NAME = "pr_review"
_SOURCE = "gh_review_requested"
_EVENT_TYPE = "gh.review_requested"
_REPO_URL_MARKER = "/repos/"

MANIFEST = TriggerManifest(
    name="gh_review_requested",
    source=_SOURCE,
    retryable_at_source=False,
)

StorageFactory = Callable[[], AbstractAsyncContextManager[aiosqlite.Connection]]


def _never_paused() -> bool:
    """Default pause check used when the container hasn't wired one."""
    return False


# Returns True if the trigger should stop (e.g. quarantined). The container
# binds this to `TriggerSupervisor.record_failure(...)` against a fresh
# storage connection; tests inject simpler closures.
PermanentFailureReporter = Callable[[str], Awaitable[bool]]


@dataclass(slots=True)
class GhReviewRequestedTrigger:
    """Long-running poller for `review-requested:<operator>` PRs."""

    gh: Any
    storage_factory: StorageFactory
    github_username: str
    poll_interval_seconds: float
    clock: Clock
    manifest: TriggerManifest = MANIFEST
    # Sync `Callable[[], bool]` — same primitive the dispatcher uses
    # (`app/pause.is_paused`). NOT the handler's async `PauseGuard` which
    # raises `QuotaError` on a single invocation; the trigger's loop wants
    # a passive flag check, not an exception-driven short-circuit.
    pause_check: Callable[[], bool] = _never_paused
    # Called only on `PermanentError` (bug-shaped failures). Transient /
    # rate-limit failures are normal and not reported. When the reporter
    # returns True the trigger stops; the operator releases via
    # `inspect triggers --unquarantine`.
    permanent_failure_reporter: PermanentFailureReporter | None = None
    # Optional GitHub-search filter fragment (e.g. `(user:rebellions-sw)` or
    # `(repo:owner/a OR repo:owner/b)`) appended to the base query so the
    # `gh.review_requested` poll only returns PRs the operator's
    # `[handlers.pr_review].allowed_repos` actually permits. Empty string
    # = no extra filter. Built in `app/registry.py` by
    # `build_search_extra_query`.
    search_extra_query: str = ""

    async def run(self, emit: EmitFn, ctx: TriggerContext) -> None:
        """Loop until cancelled. AuthError propagates and halts the daemon.

        Polls first, then sleeps — so a restart shows activity immediately
        instead of going dark for `poll_interval_seconds` (5 min by default).
        """
        del emit, ctx  # this trigger persists events directly via storage_factory.
        while True:
            if self.pause_check():
                _log.info("gh_review_requested.paused")
                await asyncio.sleep(self.poll_interval_seconds)
                continue
            try:
                emitted = await self.poll_once()
            except AuthError:
                raise
            except RateLimitError:
                _log.warning("gh_review_requested.rate_limited")
            except TransientError as exc:
                _log.warning("gh_review_requested.poll_failed", error=str(exc))
            except PermanentError as exc:
                _log.warning("gh_review_requested.poll_failed", error=str(exc))
                if (
                    self.permanent_failure_reporter is not None
                    and await self.permanent_failure_reporter(str(exc))
                ):
                    _log.error("gh_review_requested.quarantined", error=str(exc))
                    return
            else:
                if emitted:
                    _log.info("gh_review_requested.emitted", count=emitted)
            await asyncio.sleep(self.poll_interval_seconds)

    async def poll_once(self) -> int:
        """One observe-and-emit pass. Returns the number of events emitted."""
        items = await self.gh.search_review_requested(
            self.github_username, extra_query=self.search_extra_query
        )
        # `updated_at` lets us short-circuit `pr_get` for steady-state PRs:
        # if state.last_observed_at >= item.updated_at, neither the head SHA
        # nor the requested-reviewers set has changed since last cycle.
        item_updated_at: dict[tuple[str, int], str | None] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            pair = _parse_search_item(cast("dict[str, Any]", raw))
            if pair is not None:
                item_updated_at[pair] = _read_updated_at(cast("dict[str, Any]", raw))
        observed: set[tuple[str, int]] = set(item_updated_at)

        # Snapshot state once *before* the gh round-trips so the cache-hit
        # logic can skip `pr_get` for unchanged PRs. The next `async with`
        # block re-opens a connection for the upsert + emit transaction —
        # cheap, and avoids holding a connection during slow gh calls.
        async with self.storage_factory() as conn:
            persisted = await _select_all_state(conn)

        head_shas = await self._fetch_head_shas(observed, item_updated_at, persisted)

        emitted = 0
        async with self.storage_factory() as conn:
            now = self.clock.now()
            now_iso = now.astimezone(UTC).isoformat()
            keys = sorted(observed | set(persisted))
            for repo, pr_number in keys:
                in_now = (repo, pr_number) in observed
                head_sha = head_shas.get((repo, pr_number)) if in_now else None
                if in_now and head_sha is None:
                    # pr_get failed for this PR. Skip the row this cycle so a
                    # transient hiccup can't be misread as withdrawal (CASE 5)
                    # for an already-persisted PR. Next cycle retries.
                    continue
                gen, should_emit = await upsert_observation(
                    conn,
                    repo=repo,
                    pr_number=pr_number,
                    observed_now=in_now,
                    head_sha=head_sha,
                    now_iso=now_iso,
                )
                if should_emit and head_sha is not None:
                    if await _emit_event(
                        conn,
                        repo=repo,
                        pr_number=pr_number,
                        head_sha=head_sha,
                        request_gen=gen,
                        now=now,
                        now_iso=now_iso,
                    ):
                        emitted += 1
            await conn.commit()
        return emitted

    async def _fetch_head_shas(
        self,
        observed: set[tuple[str, int]],
        item_updated_at: dict[tuple[str, int], str | None],
        persisted: dict[tuple[str, int], StateRow],
    ) -> dict[tuple[str, int], str]:
        out: dict[tuple[str, int], str] = {}
        for key in observed:
            cached_sha = _cached_head_sha(persisted.get(key), item_updated_at.get(key))
            if cached_sha is not None:
                out[key] = cached_sha
                continue
            repo, pr_number = key
            try:
                payload = await self.gh.pr_get(repo, pr_number)
            except (AuthError, RateLimitError):
                raise
            except (PermanentError, TransientError) as exc:
                _log.warning(
                    "gh_review_requested.pr_get_failed",
                    repo=repo,
                    pr_number=pr_number,
                    error=str(exc),
                )
                continue
            sha = _extract_head_sha(payload)
            if sha is not None:
                out[key] = sha
        return out


def build_search_extra_query(allowed_repos: list[str]) -> str:
    """Translate `allowed_repos` glob list into a GitHub search-query fragment.

    GitHub search syntax supports `repo:owner/name`, `user:owner`, `org:owner`
    (each AND-able by space, OR-able with literal `OR`). This helper picks the
    tightest fragment expressible:

    - Empty list → ``""`` (no filter; legacy behavior).
    - All entries are `owner/*` globs → de-dup owners, emit
      ``"(user:o1 OR user:o2 ...)"``.
    - All entries are exact ``owner/name`` (no glob) → emit
      ``"(repo:o/a OR repo:o/b ...)"``.
    - Mixed (some `owner/*`, some specific) → combine: dedup owners with
      `user:`, then add specific repos that aren't already covered by an
      `owner/*` grant: ``"(user:o1 OR repo:o2/specific)"``.
    - Anything outside those forms (e.g. ``"*foo*"``, ``"*"``, no ``/``) →
      return ``""`` and rely on the handler's fnmatch gate. The trigger logs
      this so operators see why the search wasn't narrowed.

    The helper never raises — a malformed entry just falls back to handler-only
    filtering instead of breaking the poll loop.
    """
    if not allowed_repos:
        return ""

    user_owners: list[str] = []
    specific: list[str] = []
    fell_back = False
    seen_user: set[str] = set()
    seen_repo: set[str] = set()

    for raw in allowed_repos:
        entry = raw.strip()
        if not entry or "/" not in entry:
            fell_back = True
            continue
        owner, _, name = entry.partition("/")
        if not owner or not name or "*" in owner:
            fell_back = True
            continue
        if name == "*":
            key = owner.lower()
            if key not in seen_user:
                seen_user.add(key)
                user_owners.append(owner)
            continue
        if "*" in name or "?" in name or "[" in name:
            fell_back = True
            continue
        # Skip specific entries already covered by an owner/* in the same list.
        if owner.lower() in seen_user:
            continue
        key = entry.lower()
        if key in seen_repo:
            continue
        seen_repo.add(key)
        specific.append(entry)

    # Trim specifics whose owner appears later (after we've finished the pass).
    specific = [r for r in specific if r.partition("/")[0].lower() not in seen_user]

    if fell_back and not user_owners and not specific:
        _log.warning(
            "gh_review_requested.search_extra_query.fallback",
            allowed_repos=list(allowed_repos),
            reason="no expressible patterns; relying on handler-side gate",
        )
        return ""

    fragments = [f"user:{o}" for o in user_owners] + [f"repo:{r}" for r in specific]
    if not fragments:
        return ""
    return "(" + " OR ".join(fragments) + ")"


def _parse_search_item(item: dict[str, Any]) -> tuple[str, int] | None:
    number = item.get("number")
    repo_url = item.get("repository_url")
    if not isinstance(number, int) or not isinstance(repo_url, str):
        return None
    idx = repo_url.find(_REPO_URL_MARKER)
    if idx < 0:
        return None
    repo = repo_url[idx + len(_REPO_URL_MARKER) :]
    if "/" not in repo or not repo:
        return None
    return (repo, number)


def _read_updated_at(item: dict[str, Any]) -> str | None:
    raw = item.get("updated_at")
    return raw if isinstance(raw, str) and raw else None


def _iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp tolerating both `Z` and `+00:00`."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cached_head_sha(state: StateRow | None, item_updated_at: str | None) -> str | None:
    """Return the cached head SHA when the state is fresher than the search hit.

    The state row's `last_observed_at` is written every poll using `now()`,
    so it is monotonic; if it is `>=` the search-payload `updated_at`, no
    head-SHA churn nor reviewer churn has happened since last poll, and
    `pr_get` is a wasted round-trip.
    """
    if state is None or not state.head_sha:
        return None
    state_dt = _iso_datetime(state.last_observed_at)
    item_dt = _iso_datetime(item_updated_at)
    if state_dt is None or item_dt is None:
        return None
    if state_dt < item_dt:
        return None
    return state.head_sha


def _extract_head_sha(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    head = payload.get("head")
    if not isinstance(head, dict):
        return None
    sha = head.get("sha")
    if isinstance(sha, str) and sha:
        return sha
    return None


async def _select_all_state(
    conn: aiosqlite.Connection,
) -> dict[tuple[str, int], StateRow]:
    out: dict[tuple[str, int], StateRow] = {}
    async with conn.execute(
        "SELECT repo, pr_number, head_sha, request_gen, in_pending_set,"
        " last_observed_at FROM gh_review_requested_state"
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        repo = str(row["repo"])
        pr_number = int(row["pr_number"])
        out[(repo, pr_number)] = StateRow(
            repo=repo,
            pr_number=pr_number,
            head_sha=str(row["head_sha"]),
            request_gen=int(row["request_gen"]),
            in_pending_set=bool(row["in_pending_set"]),
            last_observed_at=str(row["last_observed_at"]),
        )
    return out


async def _emit_event(
    conn: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    request_gen: int,
    now: Any,
    now_iso: str,
) -> bool:
    payload: dict[str, Any] = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "request_gen": request_gen,
        "requested_at": now_iso,
    }
    # A `request_gen > 1` means the operator re-requested review at the
    # same head SHA (or a new SHA arrived). Either way the handler must
    # supersede the prior `posted` audit row instead of skipping with
    # `already_reviewed`. `force=True` is the existing supersede signal.
    if request_gen > 1:
        payload["force"] = True
    seed = f"gh-review-requested|{repo}#{pr_number}@{head_sha}#{request_gen}"
    dedup_key = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    event = make_event(type=_EVENT_TYPE, payload=payload, created_at=now)
    inserted = await outbox.insert_event(conn, event, source=_SOURCE, source_dedup_key=dedup_key)
    if not inserted:
        return False
    await outbox.enqueue_handler(conn, event_id=event.id, handler=_HANDLER_NAME, now=now)
    return True


__all__ = [
    "MANIFEST",
    "GhReviewRequestedTrigger",
    "PermanentFailureReporter",
    "StorageFactory",
    "build_search_extra_query",
]
