"""Feature 004 — `gh_pr_feedback` poll loop against a real SQLite stack.

The behaviour under test is the one the operator actually asked for: keep
polling while review comments are unanswered, and stop on your own once they
are. There is no timer and no counter behind that — it is the set difference

    live comment ids - ledger ids

going empty, so these tests drive that set from both ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from daeyeon_bot.infra import (
    pr_autofix_ledger,
    pr_feedback_state,
    pr_review_audit,
    storage,
)
from daeyeon_bot.triggers.gh_pr_feedback import GhPrFeedbackTrigger
from tests.fakes.gh_cli import FakeGh

pytestmark = pytest.mark.integration

REPO = "rebel-daeyeonlee/daeyeon-bot"
PR = 9
OPERATOR = "daeyeon-lee"
SHA = "a" * 40


# The clock starts AFTER every seeded `updated_at`, so an untouched PR reads as
# "nothing has happened since we last looked" — which is what the short-circuit
# under test keys on.
@dataclass(slots=True)
class _MovingClock:
    """Advances a minute per read so `last_observed_at` is strictly monotonic —
    the `updated_at` short-circuit compares against it. Structurally a `Clock`;
    `monotonic()` is stubbed because this trigger never reads it."""

    current: datetime

    def now(self) -> datetime:
        self.current += timedelta(minutes=1)
        return self.current

    def monotonic(self) -> float:
        return 0.0


def _trigger(db_path: Path, gh: FakeGh, **kw: Any) -> GhPrFeedbackTrigger:
    def _factory() -> Any:
        return storage.connection(db_path)

    defaults: dict[str, Any] = {
        "gh": gh,
        "storage_factory": _factory,
        "github_username": OPERATOR,
        "poll_interval_seconds": 60.0,
        "clock": _MovingClock(datetime(2026, 1, 2, tzinfo=UTC)),
    }
    defaults.update(kw)
    return GhPrFeedbackTrigger(**defaults)


async def _prepare(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    async with storage.connection(db_path) as conn:
        await storage.apply_migrations(conn)
        await conn.commit()
    return db_path


async def _events(db_path: Path) -> list[aiosqlite.Row]:
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT id, type, payload_json, source_dedup_key FROM events ORDER BY id"
        ) as cur:
            return list(await cur.fetchall())


def _seed(gh: FakeGh) -> None:
    gh.add_pr(
        REPO,
        PR,
        head_sha=SHA,
        author=OPERATOR,
        in_search_set=False,
        in_authored_set=True,
        updated_at="2026-01-01T00:00:00Z",
    )


# ── the loop starts ───────────────────────────────────────────────────────


async def test_a_new_bot_comment_emits_one_event(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="off by one")

    assert await _trigger(db_path, gh).poll_once() == 1
    rows = await _events(db_path)
    assert len(rows) == 1
    assert rows[0]["type"] == "gh.pr_feedback"
    assert '"comment_ids": [11]' in rows[0]["payload_json"].replace(" ", " ")


async def test_all_three_comment_surfaces_feed_the_pending_set(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="inline")
    gh.add_review(REPO, PR, review_id=12, author="copilot[bot]", body="summary")
    gh.add_issue_comment(REPO, PR, comment_id=13, author="colleague", body="conversation")

    assert await _trigger(db_path, gh).poll_once() == 1
    import json

    payload = json.loads((await _events(db_path))[0]["payload_json"])
    assert payload["comment_ids"] == [11, 12, 13]


# ── the loop stops ────────────────────────────────────────────────────────


async def test_no_comments_means_no_event(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)

    assert await _trigger(db_path, gh).poll_once() == 0
    assert await _events(db_path) == []
    # The PR is still tracked — that is what makes withdrawal detectable later.
    async with storage.connection(db_path) as conn:
        assert await pr_feedback_state.get_state(conn, REPO, PR) is not None


async def test_the_loop_goes_quiet_once_every_comment_is_in_the_ledger(
    tmp_path: Path,
) -> None:
    """THE termination test. Round 1 emits; the handler answers; round 2 is
    silent — no timer, no cap, just an empty set difference."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
    trigger = _trigger(db_path, gh)

    assert await trigger.poll_once() == 1

    async with storage.connection(db_path) as conn:
        await pr_autofix_ledger.record_decision(
            conn,
            repo=REPO,
            pr_number=PR,
            comment_id=11,
            comment_kind="review_comment",
            author="coderabbitai[bot]",
            verdict="accepted",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            replied=True,
        )
        await conn.commit()

    trigger.force_rescan_seconds = 0.0  # defeat the updated_at short-circuit
    assert await trigger.poll_once() == 0
    assert len(await _events(db_path)) == 1


async def test_the_loop_resumes_when_a_reviewer_pushes_back(tmp_path: Path) -> None:
    """A new comment id refills the pending set, so the bot wakes back up
    without anything having to reset state."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
    trigger = _trigger(db_path, gh, force_rescan_seconds=0.0)
    await trigger.poll_once()

    async with storage.connection(db_path) as conn:
        await pr_autofix_ledger.record_decision(
            conn,
            repo=REPO,
            pr_number=PR,
            comment_id=11,
            comment_kind="review_comment",
            author="coderabbitai[bot]",
            verdict="rejected",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            replied=True,
        )
        await conn.commit()
    assert await trigger.poll_once() == 0

    gh.add_review_comment(
        REPO,
        PR,
        comment_id=12,
        author="colleague",
        body="아니요, 진짜 버그입니다",
        in_reply_to_id=11,
    )
    assert await trigger.poll_once() == 1


# ── dedup on the pending SET ──────────────────────────────────────────────


async def test_repolling_an_unchanged_pending_set_emits_nothing(tmp_path: Path) -> None:
    """The dedup key hashes the id set, so a poll before the handler has run
    must not queue the same work twice."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
    trigger = _trigger(db_path, gh, force_rescan_seconds=0.0)

    assert await trigger.poll_once() == 1
    assert await trigger.poll_once() == 0
    assert len(await _events(db_path)) == 1


async def test_one_extra_comment_produces_a_genuinely_new_event(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
    trigger = _trigger(db_path, gh, force_rescan_seconds=0.0)
    await trigger.poll_once()

    gh.add_review_comment(REPO, PR, comment_id=12, author="coderabbitai[bot]", body="b")
    assert await trigger.poll_once() == 1
    rows = await _events(db_path)
    assert len(rows) == 2
    assert rows[0]["source_dedup_key"] != rows[1]["source_dedup_key"]


# ── scope ─────────────────────────────────────────────────────────────────


async def test_the_operators_own_comments_do_not_start_a_round(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author=OPERATOR, body="note to self")

    assert await _trigger(db_path, gh).poll_once() == 0


async def test_ignored_author_never_wakes_the_handler(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="noisy[bot]", body="nit")

    trigger = _trigger(db_path, gh, ignored_authors=["noisy[bot]"])
    assert await trigger.poll_once() == 0


async def test_a_bot_only_allowlist_ignores_human_comments(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="colleague", body="스타일")

    trigger = _trigger(db_path, gh, comment_authors=["*[bot]"])
    assert await trigger.poll_once() == 0


async def test_the_bots_own_reply_does_not_start_another_round(tmp_path: Path) -> None:
    """Replies are posted under the operator's gh identity. Without the marker
    check this is an infinite loop."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(
        REPO,
        PR,
        comment_id=11,
        author="teammate-account",
        body="🤖 **daeyeon-bot autofix** — 수정했습니다\n\n- 커밋: `abc`",
    )
    assert await _trigger(db_path, gh).poll_once() == 0


async def test_a_pr_authored_by_someone_else_is_never_observed(tmp_path: Path) -> None:
    """The trigger searches `author:<operator>` only, so another person's PR is
    not even in the observed set — the handler's author gate is the second layer."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    gh.add_pr(
        REPO, PR, head_sha=SHA, author="somebody-else", in_search_set=True, in_authored_set=False
    )
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")

    assert await _trigger(db_path, gh).poll_once() == 0


# ── rounds, caps, withdrawal ──────────────────────────────────────────────


async def test_each_emitted_event_consumes_one_round(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
    trigger = _trigger(db_path, gh, force_rescan_seconds=0.0)
    await trigger.poll_once()
    gh.add_review_comment(REPO, PR, comment_id=12, author="coderabbitai[bot]", body="b")
    await trigger.poll_once()

    async with storage.connection(db_path) as conn:
        row = await pr_feedback_state.get_state(conn, REPO, PR)
    assert row is not None and row.round == 2


async def test_max_per_cycle_caps_the_events_one_poll_can_queue(tmp_path: Path) -> None:
    """Each event can spend minutes in an agent session; a burst of reviewed PRs
    must not queue unbounded Claude work in a single cycle."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    for n in (1, 2, 3):
        gh.add_pr(
            REPO,
            n,
            head_sha=SHA,
            author=OPERATOR,
            in_search_set=False,
            in_authored_set=True,
            updated_at="2026-01-01T00:00:00Z",
        )
        gh.add_review_comment(REPO, n, comment_id=100 + n, author="coderabbitai[bot]", body="a")

    assert await _trigger(db_path, gh, max_per_cycle=2).poll_once() == 2


async def test_a_merged_pr_is_marked_withdrawn(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    trigger = _trigger(db_path, gh)
    await trigger.poll_once()

    gh.remove_from_search(REPO, PR)
    gh.remove_from_authored(REPO, PR)
    await trigger.poll_once()

    async with storage.connection(db_path) as conn:
        row = await pr_feedback_state.get_state(conn, REPO, PR)
    assert row is not None and not row.in_pending_set


# ── polling cost ──────────────────────────────────────────────────────────


async def test_an_unchanged_pr_is_not_refetched(tmp_path: Path) -> None:
    """Steady state must cost one search call, not four per PR — otherwise a
    3-minute poll burns the GitHub rate limit on PRs nobody touched."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    trigger = _trigger(db_path, gh, force_rescan_seconds=3600.0)

    await trigger.poll_once()
    assert len(gh.comment_fetches) == 3  # review comments + reviews + issue comments
    await trigger.poll_once()
    assert len(gh.comment_fetches) == 3  # short-circuited on `updated_at`


async def test_a_touched_pr_is_rescanned(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    trigger = _trigger(db_path, gh, force_rescan_seconds=3600.0)
    await trigger.poll_once()

    # A new comment bumps the PR's `updated_at` on GitHub.
    gh.add_pr(
        REPO,
        PR,
        head_sha=SHA,
        author=OPERATOR,
        in_search_set=False,
        in_authored_set=True,
        updated_at="2030-01-01T00:00:00Z",
    )
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
    assert await trigger.poll_once() == 1


# ── run-loop error mapping ────────────────────────────────────────────────


class _StopLoop(Exception):
    """Sentinel that breaks out of the trigger's `while True`."""


async def test_auth_error_propagates_and_halts_the_daemon(tmp_path: Path) -> None:
    """A bad `gh` login is a config problem, not a blip. It must reach
    `lifecycle` so the CLI exits 78 and the supervisor refuses to restart —
    swallowing it would leave a poller spinning uselessly forever."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR, auth_ok=False)
    _seed(gh)
    trigger = _trigger(db_path, gh)

    from daeyeon_bot.core.errors import AuthError

    with pytest.raises(AuthError):
        await trigger.run(_unused_emit, _unused_ctx())


async def test_rate_limit_is_survivable_and_emits_nothing(tmp_path: Path) -> None:
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR, rate_limited=True)
    _seed(gh)

    from daeyeon_bot.core.errors import RateLimitError

    with pytest.raises(RateLimitError):
        await _trigger(db_path, gh).poll_once()
    assert await _events(db_path) == []


async def test_a_paused_daemon_does_not_hit_github(tmp_path: Path) -> None:
    """PAUSE must block BEFORE the API call, not after — the flag exists to stop
    the bot consuming quota, not just to stop it acting."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")

    ticks = 0

    def _paused() -> bool:
        nonlocal ticks
        ticks += 1
        if ticks > 2:
            raise _StopLoop
        return True

    trigger = _trigger(db_path, gh, pause_check=_paused, poll_interval_seconds=0.0)
    with pytest.raises(_StopLoop):
        await trigger.run(_unused_emit, _unused_ctx())
    assert gh.comment_fetches == []
    assert await _events(db_path) == []


async def test_a_repeated_permanent_error_quarantines_the_trigger(tmp_path: Path) -> None:
    """A bug-shaped failure must eventually stop the poller instead of retrying
    the same broken call every interval until an operator notices."""
    db_path = await _prepare(tmp_path)
    from daeyeon_bot.core.errors import PermanentError

    gh = FakeGh(user_login=OPERATOR, raise_on_search=PermanentError("boom"))
    reported: list[str] = []

    async def _reporter(reason: str) -> bool:
        reported.append(reason)
        return True  # window tripped on the first failure

    trigger = _trigger(
        db_path,
        gh,
        poll_interval_seconds=0.0,
        permanent_failure_reporter=_reporter,
    )
    await trigger.run(_unused_emit, _unused_ctx())  # returns instead of looping
    assert reported == ["boom"]


async def test_a_transient_error_does_not_quarantine(tmp_path: Path) -> None:
    """Network blips are normal; only bug-shaped `PermanentError`s count toward
    the quarantine window."""
    db_path = await _prepare(tmp_path)
    from daeyeon_bot.core.errors import TransientError

    gh = FakeGh(user_login=OPERATOR, raise_on_search=TransientError("hiccup"))
    reported: list[str] = []

    async def _reporter(reason: str) -> bool:
        reported.append(reason)
        return True

    ticks = 0

    def _pause() -> bool:
        nonlocal ticks
        ticks += 1
        if ticks > 2:
            raise _StopLoop
        return False

    trigger = _trigger(
        db_path,
        gh,
        poll_interval_seconds=0.0,
        pause_check=_pause,
        permanent_failure_reporter=_reporter,
    )
    with pytest.raises(_StopLoop):
        await trigger.run(_unused_emit, _unused_ctx())
    assert reported == []


async def _unused_emit(event: Any) -> None:
    """The trigger persists events itself; `emit` is part of the protocol only."""
    raise AssertionError("gh_pr_feedback must not use the emit callback")


def _unused_ctx() -> Any:
    @dataclass(slots=True)
    class _Ctx:
        clock: Any = None

    return _Ctx()


# ── a round must cost an actual event ─────────────────────────────────────


async def test_repolling_an_unchanged_set_does_not_burn_a_round(tmp_path: Path) -> None:
    """The bug that killed ssw-bundle#5250: round 6, only three events.

    `consume_round` ran before `_emit_event`, so a re-poll of an UNCHANGED
    pending set — correctly refused by the dedup key — still moved the counter.
    A PR whose comments stay pending on purpose (what `verify_failed` leaves
    behind) then walked its own budget to zero on polling frequency alone and
    stood itself down without ever doing the work.
    """
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
    trigger = _trigger(db_path, gh, force_rescan_seconds=0.0)

    assert await trigger.poll_once() == 1
    for _ in range(4):
        assert await trigger.poll_once() == 0

    async with storage.connection(db_path) as conn:
        row = await pr_feedback_state.get_state(conn, REPO, PR)
    assert row is not None
    assert row.round == 1, f"4 no-op polls burned {row.round - 1} phantom round(s)"
    assert len(await _events(db_path)) == 1


async def test_the_round_in_the_payload_matches_the_persisted_counter(
    tmp_path: Path,
) -> None:
    """The handler's `max_rounds` gate reads the payload, so a payload round that
    disagreed with the stored one would gate on a number nothing else believes."""
    import json

    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
    trigger = _trigger(db_path, gh, force_rescan_seconds=0.0)

    await trigger.poll_once()
    gh.add_review_comment(REPO, PR, comment_id=12, author="coderabbitai[bot]", body="b")
    await trigger.poll_once()

    rounds = [json.loads(r["payload_json"])["round"] for r in await _events(db_path)]
    async with storage.connection(db_path) as conn:
        row = await pr_feedback_state.get_state(conn, REPO, PR)
    assert rounds == [1, 2]
    assert row is not None and row.round == 2


async def test_an_ignored_author_never_costs_a_round(tmp_path: Path) -> None:
    """Noise bots were consuming a round AND a Claude call each: 12 of the first
    13 rejections in production were linkback / CI-status / review-body posts."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review_comment(REPO, PR, comment_id=11, author="ssw-buildkite[bot]", body="CI ok")
    gh.add_issue_comment(REPO, PR, comment_id=12, author="linear[bot]", body="<details>link")

    trigger = _trigger(db_path, gh, ignored_authors=["ssw-buildkite[bot]", "linear[bot]"])
    assert await trigger.poll_once() == 0
    async with storage.connection(db_path) as conn:
        row = await pr_feedback_state.get_state(conn, REPO, PR)
    assert row is not None and row.round == 0


async def test_our_own_pr_review_finding_wakes_the_handler(tmp_path: Path) -> None:
    """End to end through the real audit table: a review this daemon posted must
    reach the pending set, since it arrives under the operator's own login and
    was previously invisible to autofix."""
    db_path = await _prepare(tmp_path)
    gh = FakeGh(user_login=OPERATOR)
    _seed(gh)
    gh.add_review(
        REPO,
        PR,
        review_id=5028435215,
        author=OPERATOR,
        body="**Verdict**: CONCERNS — argv에 패스워드가 노출된다",
    )

    # Without an audit row it is just the operator talking to himself.
    assert await _trigger(db_path, gh, force_rescan_seconds=0.0).poll_once() == 0

    async with storage.connection(db_path) as conn:
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at) VALUES"
            " ('rev-1','gh.review_requested',1,'gh_review_requested','k','{}','t','2026-01-01')"
        )
        await pr_review_audit.insert_audit(
            conn,
            event_id="rev-1",
            repo=REPO,
            pr_number=PR,
            head_sha=SHA,
            request_gen="1",
            status="posted",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            review_id=5028435215,
        )
        await conn.commit()

    assert await _trigger(db_path, gh, force_rescan_seconds=0.0).poll_once() == 1
