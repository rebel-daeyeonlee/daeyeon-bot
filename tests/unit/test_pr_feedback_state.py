"""Feature 004 — `gh_pr_feedback_state`, the trigger's per-PR bookkeeping."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from daeyeon_bot.infra import pr_feedback_state as state
from daeyeon_bot.infra.storage import apply_migrations, open_db

REPO = "owner/repo"


async def _db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def test_first_observation_creates_a_row_at_round_zero(tmp_path: Path) -> None:
    """Observing a PR is not the same as acting on it: `mark_observed` never
    spends a round, only `consume_round` does."""
    conn = await _db(tmp_path)
    try:
        row = await state.mark_observed(
            conn, repo=REPO, pr_number=1, head_sha="a" * 40, now_iso="2026-01-01T00:00:00+00:00"
        )
        assert row.round == 0
        assert row.in_pending_set
    finally:
        await conn.close()


async def test_consume_round_increments_monotonically(tmp_path: Path) -> None:
    conn = await _db(tmp_path)
    try:
        await state.mark_observed(
            conn, repo=REPO, pr_number=1, head_sha="a" * 40, now_iso="2026-01-01T00:00:00+00:00"
        )
        first = await state.consume_round(
            conn, repo=REPO, pr_number=1, now_iso="2026-01-01T00:01:00+00:00"
        )
        second = await state.consume_round(
            conn, repo=REPO, pr_number=1, now_iso="2026-01-01T00:02:00+00:00"
        )
        assert (first, second) == (1, 2)
    finally:
        await conn.close()


async def test_reobserving_at_a_new_sha_keeps_the_round_budget(tmp_path: Path) -> None:
    """The bot's OWN push changes the head SHA. Resetting the budget there would
    make `max_rounds` unreachable and the ping-pong brake useless."""
    conn = await _db(tmp_path)
    try:
        await state.mark_observed(
            conn, repo=REPO, pr_number=1, head_sha="a" * 40, now_iso="2026-01-01T00:00:00+00:00"
        )
        await state.consume_round(conn, repo=REPO, pr_number=1, now_iso="2026-01-01T00:01:00+00:00")
        row = await state.mark_observed(
            conn, repo=REPO, pr_number=1, head_sha="b" * 40, now_iso="2026-01-01T00:02:00+00:00"
        )
        assert row.round == 1
        assert row.head_sha == "b" * 40
    finally:
        await conn.close()


async def test_withdraw_then_reobserve_resets_the_round_budget(tmp_path: Path) -> None:
    """A PR leaving the search set and coming back means merged/reopened. The
    previous run's ping-pong budget should not follow it."""
    conn = await _db(tmp_path)
    try:
        await state.mark_observed(
            conn, repo=REPO, pr_number=1, head_sha="a" * 40, now_iso="2026-01-01T00:00:00+00:00"
        )
        await state.consume_round(conn, repo=REPO, pr_number=1, now_iso="2026-01-01T00:01:00+00:00")
        await state.mark_withdrawn(
            conn, repo=REPO, pr_number=1, now_iso="2026-01-02T00:00:00+00:00"
        )
        withdrawn = await state.get_state(conn, REPO, 1)
        assert withdrawn is not None and not withdrawn.in_pending_set

        row = await state.mark_observed(
            conn, repo=REPO, pr_number=1, head_sha="c" * 40, now_iso="2026-01-03T00:00:00+00:00"
        )
        assert row.round == 0
        assert row.in_pending_set
    finally:
        await conn.close()


async def test_prune_removes_only_dormant_rows(tmp_path: Path) -> None:
    conn = await _db(tmp_path)
    try:
        await state.mark_observed(
            conn, repo=REPO, pr_number=1, head_sha="a" * 40, now_iso="2020-01-01T00:00:00+00:00"
        )
        await state.mark_observed(
            conn, repo=REPO, pr_number=2, head_sha="b" * 40, now_iso="2020-01-01T00:00:00+00:00"
        )
        await state.mark_withdrawn(
            conn, repo=REPO, pr_number=2, now_iso="2020-01-02T00:00:00+00:00"
        )
        deleted = await state.prune_dormant(conn, older_than_iso="2021-01-01T00:00:00+00:00")
        assert deleted == 1
        assert await state.get_state(conn, REPO, 1) is not None
        assert await state.get_state(conn, REPO, 2) is None
    finally:
        await conn.close()


async def test_select_all_keys_by_repo_and_number(tmp_path: Path) -> None:
    conn = await _db(tmp_path)
    try:
        await state.mark_observed(
            conn, repo=REPO, pr_number=7, head_sha="a" * 40, now_iso="2026-01-01T00:00:00+00:00"
        )
        rows = await state.select_all(conn)
        assert set(rows) == {(REPO, 7)}
    finally:
        await conn.close()
