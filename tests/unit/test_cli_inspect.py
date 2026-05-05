"""Drive `cli/inspect.py` typer commands end-to-end via `CliRunner`.

The render helpers in this module (`_render_status`, `_event_block`,
`_format_audit_row`) only run inside the typer command callbacks, so the
fastest way to lift coverage past 60% (the `cli` target in PLAN.md §6.3)
is to invoke the CLI against a real `state.db` seeded by the regular
migration + outbox + audit paths.

One smoke-test per subcommand: status, tail, events ls/get, triggers
ls/unquarantine, pr-review (recent + filtered + invalid spec), ratelimit,
handlers ls. The non-empty branches cover the row-rendering loops; the
empty branches cover the "(no …)" fallbacks.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from typer.testing import CliRunner

from daeyeon_bot.cli.inspect import app as inspect_app
from daeyeon_bot.core.events import make_event
from daeyeon_bot.core.results import Ack
from daeyeon_bot.infra import outbox, pr_review_audit, storage


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[runtime]\nstate_dir = "{tmp_path}"\n'
        '[secrets]\nprovider = "keychain"\n'
        '[handlers.echo]\nenabled = true\naccepts = ["manual.message"]\n'
        "[triggers.manual]\nenabled = true\n"
        '[routing]\n"manual.message" = ["echo"]\n',
        encoding="utf-8",
    )
    return cfg


async def _open_db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await storage.open_db(tmp_path / "state.db")
    await storage.apply_migrations(conn)
    return conn


async def _seed_completed_run(tmp_path: Path) -> str:
    """Seed one fully-settled echo run so tail/events_ls/events_get all return rows."""
    conn = await _open_db(tmp_path)
    try:
        now = datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)
        ev = make_event(type="manual.message", payload={"m": "hi"}, created_at=now)
        await outbox.insert_event(conn, ev, source="manual", source_dedup_key=f"k-{ev.id}")
        await outbox.enqueue_handler(conn, event_id=ev.id, handler="echo", now=now)
        await conn.commit()
        job = await outbox.claim_one(conn, claimed_by="proc-A", now=now)
        assert job is not None
        await outbox.settle(
            conn,
            job=job,
            result=Ack(),
            started_at=now,
            finished_at=now,
            dedup_ttl=None,
        )
        return ev.id
    finally:
        await conn.close()


async def _seed_quarantine(tmp_path: Path, *, name: str = "manual") -> None:
    conn = await _open_db(tmp_path)
    try:
        await conn.execute(
            "INSERT INTO quarantine(trigger_name, quarantined_at, reason) VALUES (?, ?, ?)",
            (name, "2026-05-04T09:00:00+00:00", "test-seed"),
        )
        await conn.commit()
    finally:
        await conn.close()


async def _seed_audit(tmp_path: Path, *, repo: str = "octo/cat", pr_number: int = 7) -> None:
    conn = await _open_db(tmp_path)
    try:
        now = datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)
        # The pr_review_audit.event_id has FK -> events(id), so we need a parent row first.
        ev = make_event(type="pr.review.manual", payload={"pr": pr_number}, created_at=now)
        await outbox.insert_event(conn, ev, source="manual", source_dedup_key=f"audit-{ev.id}")
        await pr_review_audit.insert_audit(
            conn,
            event_id=ev.id,
            repo=repo,
            pr_number=pr_number,
            head_sha="0123456789abcdef",
            request_gen="0",
            status="posted",
            created_at=now,
            review_id=42,
            submitted_at=now,
            summary_chars=120,
            inline_comment_count=2,
            persona_skill="pr-reviewer",
        )
        await conn.commit()
    finally:
        await conn.close()


# ── status ─────────────────────────────────────────────────────────────────


def test_inspect_status_on_empty_db_prints_zero_counts(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["status", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "outbox:" in result.output
    assert "quarantined triggers:" in result.output
    assert "(none)" in result.output


def test_inspect_status_lists_quarantined_rows(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    asyncio.run(_seed_quarantine(tmp_path, name="manual"))
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["status", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "manual" in result.output
    assert "test-seed" in result.output


# ── tail ───────────────────────────────────────────────────────────────────


def test_inspect_tail_empty_db(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["tail", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "(no runs yet)" in result.output


def test_inspect_tail_with_seeded_run_prints_handler_line(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    asyncio.run(_seed_completed_run(tmp_path))
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["tail", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "echo" in result.output


# ── events ls / get ────────────────────────────────────────────────────────


def test_inspect_events_ls_empty(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["events", "ls", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "(no events)" in result.output


def test_inspect_events_ls_with_seeded_event(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    event_id = asyncio.run(_seed_completed_run(tmp_path))
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["events", "ls", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "manual.message" in result.output
    assert event_id in result.output


def test_inspect_events_get_unknown_id_exits_one(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    # Materialize the DB first so the failure is "event not found", not "db missing".
    runner = CliRunner()
    runner.invoke(inspect_app, ["status", "--config", str(cfg)])
    result = runner.invoke(inspect_app, ["events", "get", "no-such-id", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "event not found" in result.output


def test_inspect_events_get_known_id_prints_outbox_and_runs(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    event_id = asyncio.run(_seed_completed_run(tmp_path))
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["events", "get", event_id, "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert event_id in result.output
    assert "outbox:" in result.output
    assert "runs:" in result.output
    assert "echo" in result.output


# ── triggers ls / unquarantine ─────────────────────────────────────────────


def test_inspect_triggers_ls_no_triggers_configured(tmp_path: Path) -> None:
    cfg = tmp_path / "config-no-triggers.toml"
    cfg.write_text(
        f'[runtime]\nstate_dir = "{tmp_path}"\n[secrets]\nprovider = "keychain"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["triggers", "ls", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "(no triggers configured)" in result.output


def test_inspect_triggers_ls_lists_enabled(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["triggers", "ls", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "manual" in result.output
    assert "enabled" in result.output


def test_inspect_triggers_ls_marks_quarantined(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    asyncio.run(_seed_quarantine(tmp_path, name="manual"))
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["triggers", "ls", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "QUARANTINED" in result.output


def test_inspect_triggers_unquarantine_clears_row(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    asyncio.run(_seed_quarantine(tmp_path, name="manual"))
    runner = CliRunner()
    result = runner.invoke(
        inspect_app, ["triggers", "unquarantine", "manual", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    assert "cleared 1" in result.output

    # Round-trip: status should no longer list the trigger as quarantined.
    after = runner.invoke(inspect_app, ["triggers", "ls", "--config", str(cfg)])
    assert "QUARANTINED" not in after.output


# ── pr-review ──────────────────────────────────────────────────────────────


def test_inspect_pr_review_recent_empty(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["pr-review", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "(no audit rows)" in result.output


def test_inspect_pr_review_recent_with_seeded_audit(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    asyncio.run(_seed_audit(tmp_path))
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["pr-review", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "octo/cat#7" in result.output
    assert "status=posted" in result.output


def test_inspect_pr_review_filter_by_pr(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    asyncio.run(_seed_audit(tmp_path, repo="octo/cat", pr_number=7))
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["pr-review", "--pr", "octo/cat#7", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "PR octo/cat#7" in result.output
    assert "status=posted" in result.output


def test_inspect_pr_review_filter_no_match(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    # Materialize DB without seeding audit rows so the "(no audit rows for …)"
    # branch fires instead of a "db missing" error.
    runner.invoke(inspect_app, ["status", "--config", str(cfg)])
    result = runner.invoke(inspect_app, ["pr-review", "--pr", "octo/cat#99", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "(no audit rows for octo/cat#99)" in result.output


def test_inspect_pr_review_invalid_spec_missing_hash(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["pr-review", "--pr", "octo/cat", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "owner/repo#N" in result.output


def test_inspect_pr_review_invalid_spec_bad_number(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["pr-review", "--pr", "octo/cat#abc", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "PR number" in result.output or "owner/repo#N" in result.output


# ── ratelimit ──────────────────────────────────────────────────────────────


def test_inspect_ratelimit_after_migration_shows_seeded_bucket(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["ratelimit", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    # Migration 003 seeds `claude_call` with 60-token capacity.
    assert "claude_call" in result.output
    assert "60.00" in result.output


# ── handlers ls ────────────────────────────────────────────────────────────


def test_inspect_handlers_ls_lists_enabled_echo(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["handlers", "ls", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "echo" in result.output
    assert "idempotent=" in result.output
    assert "manual.message" in result.output


def test_inspect_handlers_ls_no_handlers_configured(tmp_path: Path) -> None:
    cfg = tmp_path / "config-no-handlers.toml"
    cfg.write_text(
        f'[runtime]\nstate_dir = "{tmp_path}"\n[secrets]\nprovider = "keychain"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(inspect_app, ["handlers", "ls", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "(no handlers enabled)" in result.output


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.delenv("DAEYEON_BOT_CONFIG", raising=False)
