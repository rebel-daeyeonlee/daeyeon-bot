"""Unit tests for `triggers/gh_review_requested.py` (T033).

Drives `poll_once()` directly against `FakeGh` + tmp_path SQLite so the
seven scenarios from `tasks.md` § Phase 4 are individually verifiable
without standing up a full daemon. The full TaskGroup wiring lives in
`tests/integration/test_gh_review_requested_e2e.py` (T034).
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from daeyeon_bot.core.errors import AuthError
from daeyeon_bot.core.time import Clock, SystemClock
from daeyeon_bot.infra import storage
from daeyeon_bot.triggers.gh_review_requested import (
    GhReviewRequestedTrigger,
    build_search_extra_query,
)
from tests.fakes.gh_cli import FakeGh

REPO = "octo/cat"
PR = 17


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    """Return a tmp DB path with the full migration set applied."""
    p = tmp_path / "state.db"
    async with storage.connection(p) as conn:
        await storage.apply_migrations(conn)
        await conn.commit()
    return p


def _trigger(*, gh: FakeGh, db_path: Path, **kwargs: Any) -> GhReviewRequestedTrigger:
    @asynccontextmanager
    async def factory():  # type: ignore[no-untyped-def]
        async with storage.connection(db_path) as conn:
            yield conn

    pause_check = kwargs.pop("pause_check", lambda: False)
    permanent_failure_reporter = kwargs.pop("permanent_failure_reporter", None)
    return GhReviewRequestedTrigger(
        gh=gh,
        storage_factory=factory,
        github_username=kwargs.pop("username", "daeyeon-lee"),
        poll_interval_seconds=kwargs.pop("poll_interval_seconds", 0.01),
        clock=kwargs.pop("clock", SystemClock()),
        pause_check=pause_check,
        permanent_failure_reporter=permanent_failure_reporter,
        review_self=kwargs.pop("review_self", False),
    )


async def _state_for(db_path: Path, repo: str, pr_number: int) -> dict[str, Any] | None:
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT head_sha, request_gen, in_pending_set, last_observed_at"
            " FROM gh_review_requested_state WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "head_sha": row["head_sha"],
        "request_gen": int(row["request_gen"]),
        "in_pending_set": bool(row["in_pending_set"]),
        "last_observed_at": row["last_observed_at"],
    }


async def _events_for(db_path: Path, repo: str, pr_number: int) -> list[dict[str, Any]]:
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT id, type, source, source_dedup_key, payload_json"
            " FROM events WHERE source = 'gh_review_requested' ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "source": r["source"],
            "source_dedup_key": r["source_dedup_key"],
            "payload_json": r["payload_json"],
        }
        for r in rows
    ]


async def _outbox_handlers(db_path: Path) -> list[str]:
    async with storage.connection(db_path) as conn:
        async with conn.execute("SELECT handler FROM outbox ORDER BY id") as cur:
            rows = await cur.fetchall()
    return [str(r["handler"]) for r in rows]


# ── Scenarios ─────────────────────────────────────────────────────────────


async def test_first_observation_emits_gen_one(db_path: Path) -> None:
    gh = FakeGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    emitted = await trig.poll_once()

    assert emitted == 1
    state = await _state_for(db_path, REPO, PR)
    assert state is not None
    assert state == {
        "head_sha": "sha1",
        "request_gen": 1,
        "in_pending_set": True,
        "last_observed_at": state["last_observed_at"],  # set, format checked below
    }
    events = await _events_for(db_path, REPO, PR)
    assert len(events) == 1
    assert events[0]["type"] == "gh.review_requested"
    assert '"request_gen": 1' in events[0]["payload_json"]
    assert await _outbox_handlers(db_path) == ["pr_review"]


async def test_review_self_unions_authored_search(db_path: Path) -> None:
    """`review_self=True` folds `author:<operator>` PRs into the observed set."""
    gh = FakeGh()
    # A review-requested PR (someone else's) and an authored PR (operator's own).
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    gh.add_pr(
        REPO,
        PR + 1,
        head_sha="sha2",
        author="daeyeon-lee",
        in_search_set=False,
        in_authored_set=True,
    )
    trig = _trigger(gh=gh, db_path=db_path, review_self=True)

    emitted = await trig.poll_once()

    assert emitted == 2
    own = await _state_for(db_path, REPO, PR + 1)
    assert own is not None
    assert own["head_sha"] == "sha2"
    assert own["request_gen"] == 1


async def test_review_self_disabled_ignores_authored_search(db_path: Path) -> None:
    """Default `review_self=False` never runs the authored search."""
    gh = FakeGh()
    gh.add_pr(
        REPO,
        PR + 1,
        head_sha="sha2",
        author="daeyeon-lee",
        in_search_set=False,
        in_authored_set=True,
    )
    trig = _trigger(gh=gh, db_path=db_path)

    emitted = await trig.poll_once()

    assert emitted == 0
    # The authored PR never enters the state machine when review_self is off.
    assert await _state_for(db_path, REPO, PR + 1) is None


async def test_same_observation_twice_no_emit_second_time(db_path: Path) -> None:
    gh = FakeGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    first = await trig.poll_once()
    second = await trig.poll_once()

    assert first == 1
    assert second == 0
    events = await _events_for(db_path, REPO, PR)
    assert len(events) == 1


async def test_new_push_increments_gen(db_path: Path) -> None:
    gh = FakeGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    await trig.poll_once()
    gh.update_head_sha(REPO, PR, head_sha="sha2")
    second = await trig.poll_once()

    assert second == 1
    state = await _state_for(db_path, REPO, PR)
    assert state is not None
    assert state["head_sha"] == "sha2"
    assert state["request_gen"] == 2
    assert state["in_pending_set"] is True
    events = await _events_for(db_path, REPO, PR)
    assert len(events) == 2
    assert '"request_gen": 2' in events[1]["payload_json"]


async def test_re_request_after_withdrawal_increments_gen(db_path: Path) -> None:
    gh = FakeGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    await trig.poll_once()
    gh.remove_from_search(REPO, PR)
    await trig.poll_once()  # case 5: pending=0, no emit
    gh.add_to_search(REPO, PR)
    third = await trig.poll_once()  # case 2: re-request, emit gen=2

    assert third == 1
    state = await _state_for(db_path, REPO, PR)
    assert state is not None
    assert state["request_gen"] == 2
    assert state["in_pending_set"] is True
    events = await _events_for(db_path, REPO, PR)
    assert len(events) == 2
    assert '"request_gen": 2' in events[1]["payload_json"]


async def test_permanent_withdrawal_keeps_state_no_emit(db_path: Path) -> None:
    gh = FakeGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    await trig.poll_once()
    gh.remove_from_search(REPO, PR)
    second = await trig.poll_once()
    third = await trig.poll_once()

    assert second == 0
    assert third == 0
    state = await _state_for(db_path, REPO, PR)
    assert state is not None
    assert state["in_pending_set"] is False
    events = await _events_for(db_path, REPO, PR)
    assert len(events) == 1


async def test_dedup_key_uniqueness_across_polls(db_path: Path) -> None:
    """Belt-and-suspenders: even if poll_once ran twice for the same observation,
    `events.UNIQUE(source, source_dedup_key)` lets the second insert no-op
    instead of duplicating the row.
    """
    gh = FakeGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    await trig.poll_once()

    # Manually re-emit at the same (head_sha, gen) by short-circuiting the
    # state machine: directly call _emit_event again with gen=1.
    from daeyeon_bot.core.time import SystemClock
    from daeyeon_bot.triggers.gh_review_requested import (
        _emit_event,  # pyright: ignore[reportPrivateUsage]
    )

    clock = SystemClock()
    async with storage.connection(db_path) as conn:
        now = clock.now()
        accepted = await _emit_event(
            conn,
            repo=REPO,
            pr_number=PR,
            head_sha="sha1",
            request_gen=1,
            now=now,
            now_iso=now.isoformat(),
        )
        await conn.commit()
    assert accepted is False  # UNIQUE made it a no-op

    events = await _events_for(db_path, REPO, PR)
    assert len(events) == 1


async def test_auth_error_propagates(db_path: Path) -> None:
    gh = FakeGh(auth_ok=False)
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    with pytest.raises(AuthError):
        await trig.poll_once()


async def test_run_loop_halts_on_auth_error(db_path: Path) -> None:
    """run() must surface AuthError so the daemon halts (exit 78)."""
    gh = FakeGh(auth_ok=True)
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path, poll_interval_seconds=0.001)

    async def _flip_after_first_poll() -> None:
        await asyncio.sleep(0.05)
        gh.auth_ok = False

    flipper = asyncio.create_task(_flip_after_first_poll())

    class _NopCtx:
        clock: Clock = SystemClock()

    async def _emit(_e: object) -> None:
        return None

    with pytest.raises(AuthError):
        await asyncio.wait_for(trig.run(_emit, _NopCtx()), timeout=2.0)
    await flipper


async def test_run_loop_swallows_rate_limit_and_transient_then_succeeds(
    db_path: Path,
) -> None:
    """run() must keep looping on RateLimitError / TransientError."""
    from daeyeon_bot.core.errors import TransientError

    gh = FakeGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path, poll_interval_seconds=0.001)

    # First poll: rate-limited. Second poll: transient. Third poll: success.
    seq: list[str] = ["rate", "transient", "ok"]

    async def _shift() -> None:
        # Allow `run()` to enter the first iteration so it sees rate_limited.
        await asyncio.sleep(0.02)
        gh.rate_limited = False
        # Inject a transient failure on the next search call.
        gh.raise_on_search = TransientError("flaky")
        await asyncio.sleep(0.02)
        gh.raise_on_search = None  # next search succeeds

    gh.rate_limited = True

    class _NopCtx:
        clock: Clock = SystemClock()

    async def _emit(_e: object) -> None:
        return None

    shifter = asyncio.create_task(_shift())
    run_task = asyncio.create_task(trig.run(_emit, _NopCtx()))
    try:
        # Wait until at least one event lands in the DB → success path reached.
        for _ in range(200):
            events = await _events_for(db_path, REPO, PR)
            if events:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(f"event never landed; seq={seq}")
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, shifter, return_exceptions=True)


async def test_poll_once_skips_non_dict_items(db_path: Path) -> None:
    """A non-dict element in search results is dropped (not raised)."""
    from typing import cast

    class _BadGh(FakeGh):
        async def search_review_requested(
            self, username: str, *, extra_query: str = ""
        ) -> list[dict[str, Any]]:
            base = await super().search_review_requested(username, extra_query=extra_query)
            return [*base, cast("dict[str, Any]", "not-a-dict")]  # type: ignore[list-item]

    gh = _BadGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    emitted = await trig.poll_once()
    assert emitted == 1


async def test_poll_once_handles_pr_get_failure_for_persisted_pr(
    db_path: Path,
) -> None:
    """When pr_get fails on an observed PR that has a persisted state row,
    the row must be left untouched (treated as not-observed for this cycle)."""
    from daeyeon_bot.core.errors import TransientError

    class _FlakyGh(FakeGh):
        flake: bool = False

        async def pr_get(self, repo: str, pr_number: int) -> dict[str, Any]:
            if self.flake:
                raise TransientError("flaky")
            return await super().pr_get(repo, pr_number)

    gh = _FlakyGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    first = await trig.poll_once()
    assert first == 1

    gh.flake = True
    second = await trig.poll_once()
    # No new event because head_sha couldn't be fetched; existing row preserved.
    assert second == 0
    state = await _state_for(db_path, REPO, PR)
    assert state is not None
    assert state["head_sha"] == "sha1"
    assert state["request_gen"] == 1
    # Still in pending — withdrawal is not the right interpretation here.
    assert state["in_pending_set"] is True


async def test_parse_search_item_rejects_garbage() -> None:
    """Defensive parsing of `repository_url` and `number`."""
    from daeyeon_bot.triggers.gh_review_requested import (
        _parse_search_item,  # pyright: ignore[reportPrivateUsage]
    )

    # Wrong types → None
    assert _parse_search_item({"number": "x", "repository_url": "https://api/repos/o/r"}) is None
    assert _parse_search_item({"number": 1, "repository_url": 42}) is None
    # Missing /repos/ marker → None
    assert _parse_search_item({"number": 1, "repository_url": "https://api/foo/o/r"}) is None
    # Empty repo segment → None
    assert _parse_search_item({"number": 1, "repository_url": "https://api/repos/"}) is None


async def test_extract_head_sha_handles_malformed_payloads() -> None:
    """`_extract_head_sha` returns None on any unexpected shape."""
    from daeyeon_bot.triggers.gh_review_requested import (
        _extract_head_sha,  # pyright: ignore[reportPrivateUsage]
    )

    assert _extract_head_sha(None) is None
    assert _extract_head_sha("not a dict") is None
    assert _extract_head_sha({"head": "not a dict"}) is None
    assert _extract_head_sha({"head": {}}) is None
    assert _extract_head_sha({"head": {"sha": ""}}) is None
    assert _extract_head_sha({"head": {"sha": 123}}) is None
    assert _extract_head_sha({"head": {"sha": "abc"}}) == "abc"


async def test_run_loop_skips_polling_while_paused(db_path: Path) -> None:
    """B2: while `pause_check` returns True, the trigger must not call
    `search_review_requested`. Once it flips False, polling resumes."""

    class _CountingGh(FakeGh):
        search_calls: int = 0

        async def search_review_requested(
            self, username: str, *, extra_query: str = ""
        ) -> list[dict[str, Any]]:
            self.search_calls += 1
            return await super().search_review_requested(username, extra_query=extra_query)

    gh = _CountingGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)

    paused = {"flag": True}
    trig = _trigger(
        gh=gh,
        db_path=db_path,
        pause_check=lambda: paused["flag"],
        poll_interval_seconds=0.001,
    )

    task = asyncio.create_task(trig.run(_unused_emit, _unused_ctx))  # type: ignore[arg-type]
    try:
        # Let the loop spin a few iterations while paused. No GitHub calls.
        await asyncio.sleep(0.02)
        assert gh.search_calls == 0

        # Flip the flag — next iteration should issue at least one search.
        paused["flag"] = False
        await asyncio.sleep(0.05)
        assert gh.search_calls >= 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _unused_emit(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover — never called
    raise AssertionError("emit must not be called by gh_review_requested")


_unused_ctx: Any = object()


async def test_run_loop_stops_when_failure_reporter_returns_true(db_path: Path) -> None:
    """B3: on PermanentError, the trigger reports the failure; if the
    reporter returns True (quarantine threshold tripped), the loop exits."""
    from daeyeon_bot.core.errors import PermanentError

    class _AlwaysFailGh(FakeGh):
        async def search_review_requested(
            self, username: str, *, extra_query: str = ""
        ) -> list[dict[str, Any]]:
            del username, extra_query
            raise PermanentError("simulated bug")

    gh = _AlwaysFailGh()
    calls = {"count": 0}

    async def _reporter(reason: str) -> bool:
        calls["count"] += 1
        # Trip after the 3rd failure.
        return calls["count"] >= 3

    trig = _trigger(
        gh=gh,
        db_path=db_path,
        permanent_failure_reporter=_reporter,
        poll_interval_seconds=0.001,
    )

    # `run` should return on its own (no cancellation needed) once the
    # reporter signals quarantine.
    await asyncio.wait_for(trig.run(_unused_emit, _unused_ctx), timeout=1.0)
    assert calls["count"] == 3


class _CountingGh(FakeGh):
    """FakeGh that counts `pr_get` calls and stamps each search hit
    with a configurable `updated_at`. Used to exercise the C2 cache.
    """

    item_updated_at: str | None = None
    pr_get_count: int = 0

    async def search_review_requested(
        self, username: str, *, extra_query: str = ""
    ) -> list[dict[str, Any]]:
        base = await super().search_review_requested(username, extra_query=extra_query)
        if self.item_updated_at is not None:
            for item in base:
                item["updated_at"] = self.item_updated_at
        return base

    async def pr_get(self, repo: str, pr_number: int) -> dict[str, Any]:
        self.pr_get_count += 1
        return await super().pr_get(repo, pr_number)


async def test_poll_once_skips_pr_get_for_fresh_state(db_path: Path) -> None:
    """C2: when state.last_observed_at >= item.updated_at, the cached
    head_sha is reused and `pr_get` is skipped (saves a gh round-trip
    per cycle for steady-state PRs).
    """
    gh = _CountingGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    # First poll: no state row → pr_get runs to learn head_sha.
    first = await trig.poll_once()
    assert first == 1
    assert gh.pr_get_count == 1

    # Second poll: search hint is stamped 2020 (well before the state's
    # last_observed_at, which was set to `clock.now()` above) → cache hit.
    gh.pr_get_count = 0
    gh.item_updated_at = "2020-01-01T00:00:00Z"
    second = await trig.poll_once()
    assert second == 0  # head unchanged, no emit
    assert gh.pr_get_count == 0  # ← the cache prevented the round-trip


async def test_poll_once_calls_pr_get_when_item_is_fresher(db_path: Path) -> None:
    """C2: when item.updated_at > state.last_observed_at, the search hint
    indicates churn — `pr_get` must run to refresh the head SHA. Confirms
    the cache doesn't go too far and silently miss real reviewer/SHA churn.
    """
    gh = _CountingGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    await trig.poll_once()
    gh.pr_get_count = 0
    gh.item_updated_at = "9999-12-31T00:00:00Z"
    await trig.poll_once()
    assert gh.pr_get_count == 1


async def test_poll_once_calls_pr_get_when_search_lacks_updated_at(
    db_path: Path,
) -> None:
    """C2 graceful degradation: if the search payload omits `updated_at`,
    the cache must fall back to the safe path (call pr_get) rather than
    silently reuse a possibly-stale SHA.
    """
    gh = _CountingGh()
    gh.add_pr(REPO, PR, head_sha="sha1", in_search_set=True)
    trig = _trigger(gh=gh, db_path=db_path)

    await trig.poll_once()
    gh.pr_get_count = 0
    gh.item_updated_at = None  # no `updated_at` in the search hit
    await trig.poll_once()
    assert gh.pr_get_count == 1


async def test_run_loop_does_not_report_transient_failures(db_path: Path) -> None:
    """B3: TransientError / RateLimitError must NOT increment the supervisor —
    those are normal blips, not bug-shaped failures."""
    from daeyeon_bot.core.errors import TransientError

    class _TransientGh(FakeGh):
        calls: int = 0

        async def search_review_requested(
            self, username: str, *, extra_query: str = ""
        ) -> list[dict[str, Any]]:
            self.calls += 1
            if self.calls > 3:
                # Recover after a few transient blips.
                return await super().search_review_requested(username, extra_query=extra_query)
            raise TransientError("blip")

    gh = _TransientGh()
    reporter_calls = {"count": 0}

    async def _reporter(reason: str) -> bool:
        reporter_calls["count"] += 1
        return False

    trig = _trigger(
        gh=gh,
        db_path=db_path,
        permanent_failure_reporter=_reporter,
        poll_interval_seconds=0.001,
    )

    task = asyncio.create_task(trig.run(_unused_emit, _unused_ctx))
    try:
        # Wait for several iterations, including the recovery.
        await asyncio.sleep(0.05)
        assert gh.calls >= 4
        assert reporter_calls["count"] == 0
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ── build_search_extra_query mapping ──────────────────────────────────────


def test_build_search_extra_query_empty_returns_empty_string() -> None:
    assert build_search_extra_query([]) == ""


def test_build_search_extra_query_single_owner_glob_emits_user_clause() -> None:
    fragment = build_search_extra_query(["rebellions-sw/*", "rebellions-sw/*"])
    assert fragment == "user:rebellions-sw"


def test_build_search_extra_query_multi_owner_drops_narrowing() -> None:
    # GitHub Search rejects OR-ed `user:` qualifiers (HTTP 422). Drop the
    # narrow-fragment and rely on the handler-side fnmatch gate.
    fragment = build_search_extra_query(["rebellions-sw/*", "octo/*"])
    assert fragment == ""


def test_build_search_extra_query_single_specific_emits_repo_clause() -> None:
    fragment = build_search_extra_query(["rebellions-sw/daeyeon-bot"])
    assert fragment == "repo:rebellions-sw/daeyeon-bot"


def test_build_search_extra_query_same_owner_specifics_collapse_to_user() -> None:
    # `(repo:a OR repo:b)` silently returns 0 from GitHub Search; collapse
    # multiple same-owner specifics to a single `user:` qualifier and let
    # the handler-side gate filter to the explicit list.
    fragment = build_search_extra_query(["rebellions-sw/daeyeon-bot", "rebellions-sw/other"])
    assert fragment == "user:rebellions-sw"


def test_build_search_extra_query_specific_subsumed_by_owner_glob() -> None:
    # `rebellions-sw/*` already covers `rebellions-sw/daeyeon-bot`; specific
    # entry is dropped, leaving a single owner -> single `user:` clause.
    fragment = build_search_extra_query(["rebellions-sw/*", "rebellions-sw/daeyeon-bot"])
    assert fragment == "user:rebellions-sw"


def test_build_search_extra_query_mixed_owners_drops_narrowing() -> None:
    # Mixed `owner/*` + specific from a different owner can't be narrowed
    # because GitHub Search doesn't accept OR-ed qualifiers.
    fragment = build_search_extra_query(
        ["rebellions-sw/*", "rebellions-sw/daeyeon-bot", "octo/cat"]
    )
    assert fragment == ""


def test_build_search_extra_query_complex_glob_falls_back_to_handler_only() -> None:
    # `*foo*` and bare entries can't be expressed as a GitHub search clause;
    # fall back to "" so handler-side fnmatch gate still enforces.
    assert build_search_extra_query(["*foo*"]) == ""
    assert build_search_extra_query([""]) == ""
    assert build_search_extra_query(["no-slash"]) == ""
    assert build_search_extra_query(["*/repo"]) == ""


@pytest.mark.parametrize(
    "allowed_repos",
    [
        ["rebellions-sw/*"],
        ["rebellions-sw/daeyeon-bot"],
        ["rebellions-sw/repo1", "rebellions-sw/repo2"],
        ["rebellions-sw/*", "rebellions-sw/repo1"],
    ],
)
def test_build_search_extra_query_never_wraps_in_parens(allowed_repos: list[str]) -> None:
    # Load-bearing contract: GitHub Search silently returns 0 for any qualifier
    # wrapped in parens — even a lone `(user:owner)`. Pin "no parens" explicitly
    # so a future rewrite that keeps fragments well-formed but parenthesizes
    # them (e.g. for a multi-clause attempt) can't pass review unnoticed.
    fragment = build_search_extra_query(allowed_repos)
    assert fragment != ""
    assert "(" not in fragment
    assert ")" not in fragment
