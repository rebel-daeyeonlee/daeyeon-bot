"""jira_assigned_state CRUD + state machine — T035 tests."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.infra.jira_triage_state import (
    get_state,
    prune_dormant,
    seed_cold_start,
    seed_marker_is_set,
    seed_marker_set,
    upsert_observation,
)
from daeyeon_bot.infra.storage import apply_migrations, open_db


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


@pytest.mark.asyncio
async def test_case1_first_observation_inserts_gen1_and_emits(tmp_path: Path) -> None:
    """data-model.md §5 CASE 1: row IS NULL AND observed_now → emit."""
    conn = await _open(tmp_path)
    try:
        gen, should_emit = await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2026-05-13T07:00:00Z",
        )
        await conn.commit()
        assert gen == 1
        assert should_emit is True
        row = await get_state(conn, "SSWCI-1")
        assert row is not None
        assert row.in_pending_set is True
        assert row.assignment_gen == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_case2_reentry_bumps_gen_and_emits(tmp_path: Path) -> None:
    """CASE 2: was withdrawn (in_pending_set=0), now observed → emit."""
    conn = await _open(tmp_path)
    try:
        # First observation
        await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2026-05-13T07:00:00Z",
        )
        # Withdrawn
        await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=False,
            now_iso="2026-05-13T07:05:00Z",
        )
        # Re-entered
        gen, should_emit = await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2026-05-13T07:10:00Z",
        )
        await conn.commit()
        assert gen == 2
        assert should_emit is True
        row = await get_state(conn, "SSWCI-1")
        assert row is not None
        assert row.in_pending_set is True
        assert row.assignment_gen == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_case3_stays_pending_no_emit(tmp_path: Path) -> None:
    """CASE 3: pending + still observed → touch only, no emit."""
    conn = await _open(tmp_path)
    try:
        await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2026-05-13T07:00:00Z",
        )
        gen, should_emit = await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2026-05-13T07:05:00Z",
        )
        await conn.commit()
        assert gen == 1
        assert should_emit is False
        row = await get_state(conn, "SSWCI-1")
        assert row is not None
        assert row.last_observed_at == "2026-05-13T07:05:00Z"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_case4_leaves_set_flips_flag_no_emit(tmp_path: Path) -> None:
    """CASE 4: pending + no longer observed → flip in_pending_set=0, no emit."""
    conn = await _open(tmp_path)
    try:
        await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2026-05-13T07:00:00Z",
        )
        gen, should_emit = await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=False,
            now_iso="2026-05-13T07:05:00Z",
        )
        await conn.commit()
        assert gen == 1
        assert should_emit is False
        row = await get_state(conn, "SSWCI-1")
        assert row is not None
        assert row.in_pending_set is False
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_case5_dormant_unobserved_no_op(tmp_path: Path) -> None:
    """CASE 5: dormant + still unobserved → no change."""
    conn = await _open(tmp_path)
    try:
        # Set up dormant row (via CASE 1 then CASE 4).
        await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2026-05-13T07:00:00Z",
        )
        await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=False,
            now_iso="2026-05-13T07:05:00Z",
        )
        # Now dormant.
        gen, should_emit = await upsert_observation(
            conn,
            issue_key="SSWCI-1",
            project="SSWCI",
            observed_now=False,
            now_iso="2026-05-13T07:10:00Z",
        )
        assert gen == 1
        assert should_emit is False
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_null_row_unobserved_is_noop(tmp_path: Path) -> None:
    """Strict-superset iteration tolerant: row=NULL + unobserved → no INSERT."""
    conn = await _open(tmp_path)
    try:
        gen, should_emit = await upsert_observation(
            conn,
            issue_key="GHOST-99",
            project="SSWCI",
            observed_now=False,
            now_iso="2026-05-13T07:00:00Z",
        )
        assert gen == 0
        assert should_emit is False
        assert await get_state(conn, "GHOST-99") is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_cold_start_inserts_without_events(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        inserted = await seed_cold_start(
            conn,
            observed=[("SSWCI-1", "SSWCI"), ("SSWCI-2", "SSWCI")],
            now_iso="2026-05-13T07:00:00Z",
        )
        await conn.commit()
        assert inserted == 2
        row = await get_state(conn, "SSWCI-1")
        assert row is not None
        assert row.in_pending_set is True
        assert row.assignment_gen == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_cold_start_skips_existing_rows(tmp_path: Path) -> None:
    """Second seed call doesn't double-insert."""
    conn = await _open(tmp_path)
    try:
        await seed_cold_start(
            conn,
            observed=[("SSWCI-1", "SSWCI")],
            now_iso="2026-05-13T07:00:00Z",
        )
        again = await seed_cold_start(
            conn,
            observed=[("SSWCI-1", "SSWCI"), ("SSWCI-2", "SSWCI")],
            now_iso="2026-05-13T07:05:00Z",
        )
        await conn.commit()
        assert again == 1  # only SSWCI-2 was new
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_marker_default_unset(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        assert await seed_marker_is_set(conn) is False
        await seed_marker_set(conn)
        await conn.commit()
        assert await seed_marker_is_set(conn) is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_prune_dormant_only_deletes_inactive_old_rows(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        # Pending row — must NOT be pruned.
        await upsert_observation(
            conn,
            issue_key="PEND-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2024-01-01T00:00:00Z",
        )
        # Dormant + old row.
        await upsert_observation(
            conn,
            issue_key="OLD-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2024-01-01T00:00:00Z",
        )
        await upsert_observation(
            conn,
            issue_key="OLD-1",
            project="SSWCI",
            observed_now=False,
            now_iso="2024-01-02T00:00:00Z",
        )
        # Dormant + recent row.
        await upsert_observation(
            conn,
            issue_key="RECENT-1",
            project="SSWCI",
            observed_now=True,
            now_iso="2026-05-12T00:00:00Z",
        )
        await upsert_observation(
            conn,
            issue_key="RECENT-1",
            project="SSWCI",
            observed_now=False,
            now_iso="2026-05-13T00:00:00Z",
        )
        await conn.commit()

        deleted = await prune_dormant(conn, older_than_iso="2025-01-01T00:00:00Z")
        await conn.commit()
        assert deleted == 1
        assert await get_state(conn, "PEND-1") is not None
        assert await get_state(conn, "OLD-1") is None
        assert await get_state(conn, "RECENT-1") is not None
    finally:
        await conn.close()
