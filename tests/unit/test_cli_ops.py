"""Drive `cli/ops.py` typer commands end-to-end via `CliRunner`.

Other test modules (`test_doctor.py`, `test_replay.py`, `test_prune.py`,
`test_backup.py`) cover the underlying `app/*` functions directly. This
module wraps the typer layer — `_render_*`, `typer.Exit` codes, and the
`asyncio.run` driver path — so cli/ops.py crosses the 60% coverage gate
that `D1b` set in `docs/OPTIMIZATION_PLAN.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from typer.testing import CliRunner

from daeyeon_bot.cli.ops import app as ops_app
from daeyeon_bot.core.events import make_event
from daeyeon_bot.core.results import DeadLetter
from daeyeon_bot.infra import outbox, storage


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal config.toml whose `state_dir` is `tmp_path`."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[runtime]\nstate_dir = "{tmp_path}"\n'
        '[secrets]\nprovider = "keychain"\n'
        '[handlers.echo]\nenabled = true\naccepts = ["manual.message"]\n'
        '[routing]\n"manual.message" = ["echo"]\n',
        encoding="utf-8",
    )
    return cfg


async def _open_db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await storage.open_db(tmp_path / "state.db")
    await storage.apply_migrations(conn)
    return conn


async def _seed_dead_letter(tmp_path: Path, *, handler: str = "echo") -> str:
    """Write a DLQ row to `state.db`. Returns the event_id."""
    conn = await _open_db(tmp_path)
    try:
        now = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
        ev = make_event(type="manual.message", payload={"m": "hi"}, created_at=now)
        await outbox.insert_event(conn, ev, source="manual", source_dedup_key=f"k-{ev.id}")
        await outbox.enqueue_handler(conn, event_id=ev.id, handler=handler, now=now)
        await conn.commit()
        job = await outbox.claim_one(conn, claimed_by="proc-A", now=now)
        assert job is not None
        await outbox.settle(
            conn,
            job=job,
            result=DeadLetter(reason="boom"),
            started_at=now,
            finished_at=now,
            dedup_ttl=None,
        )
        return ev.id
    finally:
        await conn.close()


# ── migrate ────────────────────────────────────────────────────────────────


def test_ops_migrate_creates_db_and_reports_schema_version(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(ops_app, ["migrate", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "schema_version=" in result.output
    assert (tmp_path / "state.db").exists()


# ── doctor ─────────────────────────────────────────────────────────────────


def test_ops_doctor_runs_and_renders_each_check(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(ops_app, ["doctor", "--config", str(cfg)])
    # Exit code may be 0 or 1 depending on token state; either way the renderer
    # must have produced one line per check.
    assert result.exit_code in (0, 1)
    out = result.output
    for name in ("state_dir", "disk", "heartbeat", "pause", "db", "token"):
        assert name in out, f"missing check in doctor output: {name}\n{out}"


def test_ops_doctor_no_config_falls_back_to_defaults(tmp_path: Path) -> None:
    runner = CliRunner()
    # Run from a tmp cwd with no config.toml so resolve_config_path returns
    # None and the renderer prints the "using defaults" hint.
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(ops_app, ["doctor"])
    assert "using defaults" in result.output


# ── replay ─────────────────────────────────────────────────────────────────


def test_ops_replay_dry_run_lists_targets_without_committing(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    import asyncio

    event_id = asyncio.run(_seed_dead_letter(tmp_path))

    runner = CliRunner()
    result = runner.invoke(ops_app, ["replay", event_id, "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "would replay" in result.output
    assert "echo" in result.output
    assert "--confirm" in result.output  # dry-run hint

    # State unchanged: row still in dead_letter, attempt_epoch still 0.
    async def _check() -> None:
        conn = await _open_db(tmp_path)
        try:
            async with conn.execute(
                "SELECT status, attempt_epoch FROM outbox WHERE event_id = ?",
                (event_id,),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row["status"] == "dead_letter"
            assert row["attempt_epoch"] == 0
        finally:
            await conn.close()

    asyncio.run(_check())


def test_ops_replay_with_confirm_resets_status_and_bumps_epoch(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    import asyncio

    event_id = asyncio.run(_seed_dead_letter(tmp_path))

    runner = CliRunner()
    result = runner.invoke(ops_app, ["replay", event_id, "--confirm", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "replayed" in result.output

    async def _check() -> None:
        conn = await _open_db(tmp_path)
        try:
            async with conn.execute(
                "SELECT status, attempt_epoch FROM outbox WHERE event_id = ?",
                (event_id,),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row["status"] == "pending"
            assert row["attempt_epoch"] == 1
        finally:
            await conn.close()

    asyncio.run(_check())


def test_ops_replay_unknown_event_exits_one(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    # Migrate first so the DB exists; otherwise replay hits a different code
    # path (db missing) that's tested elsewhere.
    runner = CliRunner()
    runner.invoke(ops_app, ["migrate", "--config", str(cfg)])
    result = runner.invoke(ops_app, ["replay", "no-such-event", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "no outbox rows match" in result.output


def test_ops_replay_handler_filter(tmp_path: Path) -> None:
    """`--handler` narrows the plan to a single handler."""
    cfg = _write_config(tmp_path)
    import asyncio

    event_id = asyncio.run(_seed_dead_letter(tmp_path, handler="echo"))

    runner = CliRunner()
    result = runner.invoke(ops_app, ["replay", event_id, "--handler", "echo", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "echo" in result.output


# ── prune ──────────────────────────────────────────────────────────────────


def test_ops_prune_reports_zero_counts_on_fresh_db(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(ops_app, ["migrate", "--config", str(cfg)])
    result = runner.invoke(ops_app, ["prune", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "runs deleted: 0" in result.output
    assert "dedup_keys deleted: 0" in result.output


# ── backup ─────────────────────────────────────────────────────────────────


def test_ops_backup_writes_snapshot_under_state_dir(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(ops_app, ["migrate", "--config", str(cfg)])
    result = runner.invoke(ops_app, ["backup", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "snapshot:" in result.output
    snapshots = list((tmp_path / "backups").glob("state-*.db"))
    assert len(snapshots) == 1


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Make sure stray DAEYEON_BOT_CONFIG from the dev shell can't leak in."""
    monkeypatch.delenv("DAEYEON_BOT_CONFIG", raising=False)
