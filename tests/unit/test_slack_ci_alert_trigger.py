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
    """Scripted history. `messages[channel]` is the full message list."""

    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

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


def _msg(ts: str, *, candidate: bool = True) -> dict[str, Any]:
    text = _LINK.format(n=int(float(ts))) if candidate else "human chatter, no link"
    return {"ts": ts, "user": _BOT if candidate else "UHUMAN", "text": text}


def _trigger(
    slack: _FakeSlack, db_path: Path, clock: _Clock, *, max_per_cycle: int = 20
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
