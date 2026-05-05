"""Atomic token-bucket — concurrent take(), refill arithmetic, missing bucket.

The module is the only sanctioned doorway to a row in `ratelimit_buckets`
(CONTRACTS §5). These tests pin the contract: a single atomic UPDATE per
take, time-based refill clipped to capacity, fail-closed on misconfigured
bucket names.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.app import ratelimit
from daeyeon_bot.infra import storage


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await storage.open_db(tmp_path / "state.db")
    await storage.apply_migrations(conn)
    return conn


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── seed + snapshot ─────────────────────────────────────────────────────────


async def test_migration_003_seeds_claude_call_bucket(tmp_path: Path) -> None:
    """The seed row from migration 003 must be present after a clean migrate."""
    conn = await _open(tmp_path)
    try:
        rows = await ratelimit.snapshot(conn)
    finally:
        await conn.close()

    names = {r[0] for r in rows}
    assert ratelimit.CLAUDE_CALL_BUCKET in names
    name, tokens, capacity, refill, _last = next(
        r for r in rows if r[0] == ratelimit.CLAUDE_CALL_BUCKET
    )
    assert name == "claude_call"
    assert capacity == 60.0
    assert refill == 1.0
    # Seed value; first take() refills to capacity from the epoch-zero `last_refill`.
    assert tokens == 60.0


# ── upsert: capacity/refill knobs without resetting tokens ──────────────────


async def test_upsert_bucket_preserves_tokens_on_conflict(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        # Drain a few tokens so we can prove tokens survive the UPSERT.
        now = datetime.now(tz=UTC)
        for _ in range(5):
            assert await ratelimit.take(conn, ratelimit.CLAUDE_CALL_BUCKET, now_iso=_iso(now))
        await conn.commit()

        # Snapshot tokens immediately before UPSERT.
        async with conn.execute(
            "SELECT tokens FROM ratelimit_buckets WHERE name = ?",
            (ratelimit.CLAUDE_CALL_BUCKET,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        before = float(row["tokens"])

        # Bump capacity + refill via the public API. Tokens must NOT reset to capacity.
        await ratelimit.upsert_bucket(
            conn,
            name=ratelimit.CLAUDE_CALL_BUCKET,
            capacity=120.0,
            refill_per_sec=2.0,
            now_iso=_iso(now),
        )
        await conn.commit()

        rows = await ratelimit.snapshot(conn)
        _name, tokens_after, capacity_after, refill_after, _last = next(
            r for r in rows if r[0] == ratelimit.CLAUDE_CALL_BUCKET
        )
        assert capacity_after == 120.0
        assert refill_after == 2.0
        # Tokens unchanged by UPSERT (the seeded value is preserved).
        assert tokens_after == before
    finally:
        await conn.close()


async def test_upsert_bucket_creates_when_missing(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        now = datetime.now(tz=UTC)
        await ratelimit.upsert_bucket(
            conn, name="custom", capacity=10.0, refill_per_sec=0.5, now_iso=_iso(now)
        )
        await conn.commit()
        rows = await ratelimit.snapshot(conn)
    finally:
        await conn.close()

    _name, tokens, capacity, refill, _last = next(r for r in rows if r[0] == "custom")
    assert capacity == 10.0
    assert refill == 0.5
    # Fresh insert seeds tokens=capacity so the bucket boots full.
    assert tokens == 10.0


# ── take(): refill arithmetic + capacity cap ────────────────────────────────


async def test_take_decrements_one_token_per_call(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        # Frozen `now` so time-based refill contributes ~0.
        now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
        await ratelimit.upsert_bucket(
            conn, name="b1", capacity=3.0, refill_per_sec=0.0, now_iso=_iso(now)
        )
        await conn.commit()

        # Three takes succeed, fourth fails.
        assert await ratelimit.take(conn, "b1", now_iso=_iso(now))
        assert await ratelimit.take(conn, "b1", now_iso=_iso(now))
        assert await ratelimit.take(conn, "b1", now_iso=_iso(now))
        assert not await ratelimit.take(conn, "b1", now_iso=_iso(now))
    finally:
        await conn.close()


async def test_take_refills_over_time_clipped_to_capacity(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        t0 = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
        await ratelimit.upsert_bucket(
            conn, name="b1", capacity=5.0, refill_per_sec=1.0, now_iso=_iso(t0)
        )
        # Drain to zero.
        for _ in range(5):
            assert await ratelimit.take(conn, "b1", now_iso=_iso(t0))
        assert not await ratelimit.take(conn, "b1", now_iso=_iso(t0))

        # 2 seconds later: refill = 2.0 → exactly 2 successful takes.
        t1 = t0 + timedelta(seconds=2)
        assert await ratelimit.take(conn, "b1", now_iso=_iso(t1))
        assert await ratelimit.take(conn, "b1", now_iso=_iso(t1))
        assert not await ratelimit.take(conn, "b1", now_iso=_iso(t1))

        # 1000 seconds later: refill huge but clipped to capacity=5.
        t2 = t1 + timedelta(seconds=1000)
        for _ in range(5):
            assert await ratelimit.take(conn, "b1", now_iso=_iso(t2))
        assert not await ratelimit.take(conn, "b1", now_iso=_iso(t2))
    finally:
        await conn.close()


async def test_take_missing_bucket_returns_false(tmp_path: Path) -> None:
    """Fail-closed: an unknown bucket name returns False, never silently passes."""
    conn = await _open(tmp_path)
    try:
        now = datetime.now(tz=UTC)
        result = await ratelimit.take(conn, "no-such-bucket", now_iso=_iso(now))
    finally:
        await conn.close()
    assert result is False


# ── concurrent take(): atomicity under race ────────────────────────────────


async def test_concurrent_take_respects_capacity(tmp_path: Path) -> None:
    """10 parallel coroutines, capacity=5 → exactly 5 should succeed.

    `take()` is one atomic UPDATE so SQLite's row-level write lock serializes
    them; the WHERE clause re-evaluates the refill arithmetic per attempt,
    so we can't end up with rowcount=1 from more callers than there are
    tokens.
    """
    conn = await _open(tmp_path)
    try:
        t0 = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
        await ratelimit.upsert_bucket(
            conn, name="b1", capacity=5.0, refill_per_sec=0.0, now_iso=_iso(t0)
        )
        await conn.commit()

        # All takes share the same `now` so refill contributes 0; the only
        # source of "winners" is the initial 5 tokens.
        results = await asyncio.gather(
            *(ratelimit.take(conn, "b1", now_iso=_iso(t0)) for _ in range(10))
        )
    finally:
        await conn.close()

    assert sum(results) == 5


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.delenv("DAEYEON_BOT_CONFIG", raising=False)
