"""slack_ci_alert polling trigger — cursor state machine (feature 003 P2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daeyeon_bot.infra import storage
from daeyeon_bot.infra.slack import HistoryPage
from daeyeon_bot.infra.slack_ci_alert_state import get_cursor
from daeyeon_bot.infra.storage import apply_migrations, open_db
from daeyeon_bot.triggers.slack_ci_alert import SlackCiAlertTrigger

_CH = "C1"
_BOT = "U069J27G2G6"  # known alert bot
_BASE = 1_781_800_000.0  # ~2026-06-19 UTC, so Slack ts are realistic vs the clock
_LINK = "see https://github.com/rebellions-sw/ssw-bundle/actions/runs/{n}"


def _ts(offset: float) -> str:
    return f"{_BASE + offset:.1f}"


def _dt(offset: float) -> datetime:
    return datetime.fromtimestamp(_BASE + offset, tz=UTC)


@dataclass(slots=True)
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.value.timestamp()


@dataclass(slots=True)
class _FakeSlack:
    """Scripted history. `messages[channel]` is the top-level message list;
    `thread_replies[parent_ts]` holds reply messages (excluding the parent),
    mirroring Slack where replies surface only via `conversations.replies`."""

    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    thread_replies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    replies_calls: list[str] = field(default_factory=list)

    async def history(
        self,
        channel_id: str,
        *,
        oldest: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        msgs = self.messages.get(channel_id, [])
        if oldest is None:  # _latest_ts probe (limit=1 → newest first)
            newest = sorted(msgs, key=lambda m: float(m["ts"]), reverse=True)
            return HistoryPage(messages=newest[:1], next_cursor=None)
        fresh = [m for m in msgs if float(m["ts"]) > float(oldest)]
        fresh.sort(key=lambda m: float(m["ts"]), reverse=True)  # Slack: newest-first
        return HistoryPage(messages=fresh, next_cursor=None)

    async def replies(
        self, channel_id: str, *, thread_ts: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.replies_calls.append(str(thread_ts))
        parent = next(
            (m for m in self.messages.get(channel_id, []) if str(m["ts"]) == str(thread_ts)),
            None,
        )
        out: list[dict[str, Any]] = [parent] if parent is not None else []
        out.extend(self.thread_replies.get(str(thread_ts), []))
        return out


def _msg(ts: str, *, candidate: bool = True) -> dict[str, Any]:
    text = _LINK.format(n=int(float(ts))) if candidate else "human chatter, no link"
    return {"ts": ts, "user": _BOT if candidate else "UHUMAN", "text": text}


def _trigger(
    slack: _FakeSlack,
    db_path: Path,
    clock: _Clock,
    *,
    max_per_cycle: int = 20,
    thread_lookback_seconds: float = 0.0,
) -> SlackCiAlertTrigger:
    def _sf() -> Any:
        return storage.connection(db_path)

    return SlackCiAlertTrigger(
        slack=slack,
        storage_factory=_sf,
        channels=(_CH,),
        poll_interval_seconds=120.0,
        max_per_cycle=max_per_cycle,
        staleness_seconds=21600.0,
        thread_lookback_seconds=thread_lookback_seconds,
        clock=clock,
    )


async def _count_events(db_path: Path) -> int:
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE source = 'slack_ci_alert'"
        ) as cur:
            row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def _cursor_ts(db_path: Path) -> str | None:
    async with storage.connection(db_path) as c:
        cur = await get_cursor(c, _CH)
    return cur.last_seen_ts if cur else None


async def _init_db(tmp_path: Path) -> Path:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    await conn.close()
    return tmp_path / "state.db"


async def test_cold_start_seeds_no_emit(tmp_path: Path) -> None:
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1)), _msg(_ts(2))]})
    trig = _trigger(slack, db_path, _Clock(_dt(100)))

    out = await trig.poll_once()
    assert (out.seeded, out.emitted) == (1, 0)
    assert await _count_events(db_path) == 0
    assert await _cursor_ts(db_path) == _ts(2)  # seeded to latest


async def test_cold_start_empty_channel_no_row(tmp_path: Path) -> None:
    db_path = await _init_db(tmp_path)
    trig = _trigger(_FakeSlack(messages={_CH: []}), db_path, _Clock(_dt(100)))
    out = await trig.poll_once()
    assert out.seeded == 0
    assert await _cursor_ts(db_path) is None  # no fabricated cursor


async def test_emit_on_new_candidate_then_dedup(tmp_path: Path) -> None:
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    trig = _trigger(slack, db_path, _Clock(_dt(100)))

    await trig.poll_once()  # cold-start seed at _ts(1)
    slack.messages[_CH].append(_msg(_ts(50)))  # new candidate
    out = await trig.poll_once()
    assert out.emitted == 1
    assert await _count_events(db_path) == 1
    assert await _cursor_ts(db_path) == _ts(50)

    out2 = await trig.poll_once()  # no new messages → no double emit
    assert out2.emitted == 0
    assert await _count_events(db_path) == 1


async def test_advance_past_chatter(tmp_path: Path) -> None:
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    trig = _trigger(slack, db_path, _Clock(_dt(100)))
    await trig.poll_once()  # seed
    slack.messages[_CH].append(_msg(_ts(60), candidate=False))  # pure chatter
    out = await trig.poll_once()
    assert out.emitted == 0
    assert await _count_events(db_path) == 0
    assert await _cursor_ts(db_path) == _ts(60)  # advanced past chatter


async def test_late_reply_to_passed_parent_recovered(tmp_path: Path) -> None:
    """#1: a human posts 'premerge fail' (no link) → it passes the cursor as
    chatter; the run-URL reply lands later. `conversations.history` never returns
    that reply nor resurfaces the parent, so without the late-reply re-check the
    alert is lost. With thread_lookback set, the next poll recovers it — once."""
    db_path = await _init_db(tmp_path)
    parent = _msg(_ts(100), candidate=False)  # 'premerge fail', no link yet
    slack = _FakeSlack(messages={_CH: [parent]})
    trig = _trigger(slack, db_path, _Clock(_dt(200)), thread_lookback_seconds=3600.0)

    await trig.poll_once()  # cold-start seed at _ts(100); parent now ≤ cursor
    assert await _count_events(db_path) == 0

    # The run-URL reply arrives in the parent's thread (not a top-level message).
    slack.thread_replies[_ts(100)] = [_msg(_ts(150))]
    parent["reply_count"] = 1
    parent["latest_reply"] = _ts(150)

    out = await trig.poll_once()
    assert out.late == 1 and out.emitted == 0  # recovered by the late pass
    assert await _count_events(db_path) == 1
    assert await _cursor_ts(db_path) == _ts(100)  # cursor NOT moved by the late pass

    out2 = await trig.poll_once()  # idempotent — dedup blocks a second emit
    assert out2.late == 0
    assert await _count_events(db_path) == 1


async def test_late_reply_disabled_when_lookback_zero(tmp_path: Path) -> None:
    """thread_lookback_seconds=0 (default) → late-reply recovery is off; a reply to
    an already-passed parent stays lost (documents the opt-in boundary)."""
    db_path = await _init_db(tmp_path)
    parent = _msg(_ts(100), candidate=False)
    slack = _FakeSlack(messages={_CH: [parent]})
    trig = _trigger(slack, db_path, _Clock(_dt(200)))  # lookback defaults to 0

    await trig.poll_once()  # seed at _ts(100)
    slack.thread_replies[_ts(100)] = [_msg(_ts(150))]
    parent["reply_count"] = 1
    parent["latest_reply"] = _ts(150)

    out = await trig.poll_once()
    assert out.late == 0
    assert await _count_events(db_path) == 0


async def test_max_per_cycle_cap(tmp_path: Path) -> None:
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    trig = _trigger(slack, db_path, _Clock(_dt(100)), max_per_cycle=2)
    await trig.poll_once()  # seed
    for off in (10, 20, 30, 40):  # 4 new candidates
        slack.messages[_CH].append(_msg(_ts(off)))
    out = await trig.poll_once()
    assert out.emitted == 2  # capped
    assert await _cursor_ts(db_path) == _ts(20)  # last EMITTED, not newest

    out2 = await trig.poll_once()  # remainder next cycle
    assert out2.emitted == 2
    assert await _count_events(db_path) == 4


async def test_staleness_reseed(tmp_path: Path) -> None:
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    clock = _Clock(_dt(100))
    trig = _trigger(slack, db_path, clock)
    await trig.poll_once()  # seed at _ts(1)

    slack.messages[_CH].append(_msg(_ts(200)))  # a candidate arrives
    clock.value = _dt(40000)  # >6h after the cursor → staleness re-seed
    out = await trig.poll_once()
    assert out.reseeded == 1
    assert out.emitted == 0  # no back-fill of stale runs
    assert await _count_events(db_path) == 0
    assert await _cursor_ts(db_path) == _ts(200)  # re-seeded to latest


async def test_non_candidate_unknown_author_filtered(tmp_path: Path) -> None:
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    trig = _trigger(slack, db_path, _Clock(_dt(100)))
    await trig.poll_once()  # seed
    # human message, unknown author, no run link → not a candidate.
    slack.messages[_CH].append({"ts": _ts(70), "user": "UHUMAN", "text": "approve 했네요 ^^;;"})
    out = await trig.poll_once()
    assert out.emitted == 0
    assert await _cursor_ts(db_path) == _ts(70)


async def _latest_event_payload(db_path: Path) -> dict[str, Any]:
    import json

    async with storage.connection(db_path) as c:
        async with c.execute(
            "SELECT payload_json FROM events WHERE source = 'slack_ci_alert'"
            " ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    return json.loads(row["payload_json"])


async def test_thread_reply_run_link_makes_candidate(tmp_path: Path) -> None:
    """A human 'premerge fail' parent with no link, but a reply carrying the run
    URL, is a candidate. The event targets the PARENT thread and its raw_blob
    carries the reply's run link so the handler can resolve the run."""
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    trig = _trigger(slack, db_path, _Clock(_dt(100)))
    await trig.poll_once()  # seed at _ts(1)

    parent = {
        "ts": _ts(50),
        "user": "UHUMAN",
        "text": "[TEST] CR03 premerge fail",
        "reply_count": 1,
    }
    slack.messages[_CH].append(parent)
    slack.thread_replies[_ts(50)] = [
        {
            "ts": _ts(51),
            "user": "UHUMAN",
            "text": "see https://github.com/rebellions-sw/ssw-bundle/actions/runs/27123448635/job/8?pr=3690",
        }
    ]

    out = await trig.poll_once()
    assert out.emitted == 1
    assert await _count_events(db_path) == 1
    payload = await _latest_event_payload(db_path)
    assert payload["message_ts"] == _ts(50)  # reply in the parent's thread
    assert "27123448635" in payload["raw_blob"]  # run link pulled from the reply
    assert await _cursor_ts(db_path) == _ts(50)


async def test_thread_without_run_link_is_not_candidate(tmp_path: Path) -> None:
    """A threaded parent whose replies carry no run link stays non-candidate;
    the cursor advances past it with no emit."""
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    trig = _trigger(slack, db_path, _Clock(_dt(100)))
    await trig.poll_once()  # seed

    parent = {"ts": _ts(60), "user": "UHUMAN", "text": "도와주세요", "reply_count": 1}
    slack.messages[_CH].append(parent)
    slack.thread_replies[_ts(60)] = [{"ts": _ts(61), "user": "UHUMAN", "text": "확인 부탁요"}]

    out = await trig.poll_once()
    assert out.emitted == 0
    assert await _count_events(db_path) == 0
    assert await _cursor_ts(db_path) == _ts(60)


async def test_no_replies_skips_thread_fetch(tmp_path: Path) -> None:
    """A non-candidate message with reply_count 0 must NOT trigger a replies()
    fetch (API-call minimization)."""
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    trig = _trigger(slack, db_path, _Clock(_dt(100)))
    await trig.poll_once()  # seed
    slack.messages[_CH].append(
        {"ts": _ts(70), "user": "UHUMAN", "text": "그냥 잡담"}
    )  # no reply_count

    out = await trig.poll_once()
    assert out.emitted == 0
    assert slack.replies_calls == []  # replies() never called for a reply-less message


async def test_quiet_but_polled_channel_does_not_reseed(tmp_path: Path) -> None:
    """A channel quiet for >staleness (so its last MESSAGE is old) must still emit
    the next alert as long as the daemon kept polling (updated_at stays fresh).
    Staleness is a daemon-outage guard, not a quiet-channel guard — regression for
    the bug where an aged cursor ate the first alert after a lull."""
    db_path = await _init_db(tmp_path)
    slack = _FakeSlack(messages={_CH: [_msg(_ts(1))]})
    clock = _Clock(_dt(100))
    trig = _trigger(slack, db_path, clock)
    await trig.poll_once()  # seed at _ts(1), updated_at=_dt(100)

    # Keep polling every <staleness while the channel is silent — updated_at stays
    # fresh even though _ts(1) ages well past 6h.
    for t in (10_000, 20_000, 29_000):
        clock.value = _dt(t)
        out = await trig.poll_once()
        assert out.reseeded == 0  # actively polled → never a "stale" gap

    # A new alert arrives; only ~5s since the last poll → not an outage.
    clock.value = _dt(29_005)
    slack.messages[_CH].append(_msg(_ts(29_004)))
    out = await trig.poll_once()
    assert out.reseeded == 0
    assert out.emitted == 1  # the alert is triaged, NOT eaten by a stale reseed
    assert await _count_events(db_path) == 1
    assert await _cursor_ts(db_path) == _ts(29_004)
