"""Feedback loop: reaction classification + collection (feature 003 D)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from daeyeon_bot.infra.ci_feedback import classify_reactions, collect_feedback
from daeyeon_bot.infra.ci_triage_audit import feedback_stats, insert_audit
from daeyeon_bot.infra.storage import apply_migrations, open_db

_NOW = datetime(2026, 6, 24, 5, 0, 0, tzinfo=UTC)


def test_classify_reactions() -> None:
    assert classify_reactions(["white_check_mark"]) == ("correct", "white_check_mark")
    assert classify_reactions(["x"]) == ("incorrect", "x")
    assert classify_reactions(["eyes", "tada"]) is None
    assert classify_reactions([]) is None
    # conflicting → unsure
    v = classify_reactions(["white_check_mark", "x"])
    assert v is not None and v[0] == "unsure"


class _FakeReactions:
    def __init__(self, mapping: dict[str, list[tuple[str, int]]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def reactions_get(self, channel_id: str, timestamp: str) -> list[tuple[str, int]]:
        self.calls.append(timestamp)
        return self.mapping.get(timestamp, [])


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def _seed_posted(
    conn: aiosqlite.Connection, *, ev: str, ts: str, attribution: str = "infra_env"
) -> None:
    await conn.execute(
        "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
        " payload_json, trace_id, created_at) VALUES"
        f" ('{ev}', 'slack.ci_alert', 1, 'x', '{ev}', '{{}}', 'tr', '2026-06-24T00:00:00Z')"
    )
    await insert_audit(
        conn,
        event_id=ev,
        channel_id="C1",
        message_ts=ts,
        status="posted",
        created_at=_NOW,
        attribution=attribution,
        posted_channel_id="C1",
        posted_message_ts=ts,
    )
    await conn.commit()


async def test_collect_feedback_records_verdicts(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        await _seed_posted(conn, ev="e1", ts="100.1", attribution="infra_env")
        await _seed_posted(conn, ev="e2", ts="100.2", attribution="product_regression")
        await _seed_posted(conn, ev="e3", ts="100.3")  # no reactions → stays unrated
        slack = _FakeReactions(
            {
                "100.1": [("white_check_mark", 1)],
                "100.2": [("x", 1)],
                "100.3": [("eyes", 1)],
            }
        )
        updated = await collect_feedback(conn, slack, now=_NOW)
        assert updated == 2

        stats = await feedback_stats(conn, since_iso="2026-06-01T00:00:00Z")
        assert stats.posted == 3
        assert stats.rated == 2
        assert stats.correct == 1 and stats.incorrect == 1
        assert stats.accuracy == 0.5
        assert stats.by_attribution["infra_env"] == (1, 1)
        assert stats.by_attribution["product_regression"] == (1, 0)

        # second pass: already-rated rows are skipped (only the unrated 100.3 scanned)
        slack.calls.clear()
        again = await collect_feedback(conn, slack, now=_NOW)
        assert again == 0
        assert slack.calls == ["100.3"]
    finally:
        await conn.close()
