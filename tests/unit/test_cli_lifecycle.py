"""Drive `cli/lifecycle.py` typer commands via `CliRunner`.

The integration test in `tests/integration/test_pause_resume.py` already
exercises a real boot+stop. This module wraps the thin CLI layer:
exit-code mapping (`AlreadyRunningError → 75`, `AuthError/ConfigError → 78`),
the pause/resume flag dance, and the pidfile-signal commands. Keeps
cli/lifecycle.py over the 60% coverage gate set by `D1a`.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from daeyeon_bot.app.lock import AlreadyRunningError
from daeyeon_bot.cli import lifecycle as cli_lifecycle
from daeyeon_bot.cli.lifecycle import app as lifecycle_app
from daeyeon_bot.cli.main import app as main_app
from daeyeon_bot.core.errors import AuthError, ConfigError


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[runtime]\nstate_dir = "{tmp_path}"\n'
        '[secrets]\nprovider = "keychain"\n'
        '[handlers.echo]\nenabled = true\naccepts = ["manual.message"]\n'
        '[routing]\n"manual.message" = ["echo"]\n',
        encoding="utf-8",
    )
    return cfg


# ── pause / resume ─────────────────────────────────────────────────────────


def test_lifecycle_pause_creates_flag_then_idempotent(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()

    first = runner.invoke(lifecycle_app, ["pause", "--config", str(cfg)])
    assert first.exit_code == 0, first.output
    assert "paused:" in first.output
    assert (tmp_path / "PAUSE").exists()

    # Re-running while already paused must report idempotent behavior.
    second = runner.invoke(lifecycle_app, ["pause", "--config", str(cfg)])
    assert second.exit_code == 0, second.output
    assert "already paused:" in second.output


def test_lifecycle_resume_removes_flag_then_no_op(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    (tmp_path / "PAUSE").touch()
    runner = CliRunner()

    first = runner.invoke(lifecycle_app, ["resume", "--config", str(cfg)])
    assert first.exit_code == 0, first.output
    assert "resumed:" in first.output
    assert not (tmp_path / "PAUSE").exists()

    # Already resumed → no-op message.
    second = runner.invoke(lifecycle_app, ["resume", "--config", str(cfg)])
    assert second.exit_code == 0, second.output
    assert "not paused:" in second.output


# ── stop / reload-config (pidfile signal path) ─────────────────────────────


def test_lifecycle_stop_no_pidfile_exits_one(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(lifecycle_app, ["stop", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "no pidfile" in result.output


def test_lifecycle_stop_unreadable_pidfile_exits_one(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    (tmp_path / "daeyeon-bot.pid").write_text("not-an-int\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(lifecycle_app, ["stop", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "unreadable pidfile" in result.output


def test_lifecycle_stop_stale_pidfile_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path)
    (tmp_path / "daeyeon-bot.pid").write_text("99999\n", encoding="utf-8")

    def _kill_missing(pid: int, sig: int) -> None:
        del pid, sig
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", _kill_missing)
    runner = CliRunner()
    result = runner.invoke(lifecycle_app, ["stop", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "no process with pid" in result.output


def test_lifecycle_stop_sends_sigterm_to_recorded_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path)
    (tmp_path / "daeyeon-bot.pid").write_text("4242\n", encoding="utf-8")

    sent: list[tuple[int, int]] = []

    def _record(pid: int, sig: int) -> None:
        sent.append((pid, sig))

    monkeypatch.setattr(os, "kill", _record)
    runner = CliRunner()
    result = runner.invoke(lifecycle_app, ["stop", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert sent == [(4242, signal.SIGTERM)]
    assert "SIGTERM sent to pid 4242" in result.output


def test_lifecycle_reload_config_signals_and_logs_supervisor_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path)
    (tmp_path / "daeyeon-bot.pid").write_text("4242\n", encoding="utf-8")

    sent: list[tuple[int, int]] = []

    def _record(pid: int, sig: int) -> None:
        sent.append((pid, sig))

    monkeypatch.setattr(os, "kill", _record)
    runner = CliRunner()
    result = runner.invoke(lifecycle_app, ["reload-config", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert sent == [(4242, signal.SIGTERM)]
    # Stderr hint about supervisor restart goes through `err=True`. CliRunner
    # captures both into `output` by default.
    assert "supervisor" in result.output


# ── run (boot exit-code mapping) ───────────────────────────────────────────
# `run` is registered on the root `cli.main.app`, not `lifecycle_app`.
# Drive it through the root app so typer's exit-code conversion fires.


def _patch_boot(
    monkeypatch: pytest.MonkeyPatch,
    raiser: Callable[..., Awaitable[None]],
) -> None:
    monkeypatch.setattr(cli_lifecycle, "boot", raiser)


def test_lifecycle_run_already_running_exits_75(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path)

    async def _raise(_options: Any) -> None:
        raise AlreadyRunningError(path=tmp_path / "daeyeon-bot.pid", holder_pid=4242)

    _patch_boot(monkeypatch, _raise)
    runner = CliRunner()
    result = runner.invoke(main_app, ["run", "--config", str(cfg)])
    assert result.exit_code == 75
    assert "daeyeon-bot:" in result.output


def test_lifecycle_run_auth_error_exits_78(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path)

    async def _raise(_options: Any) -> None:
        raise AuthError("token expired")

    _patch_boot(monkeypatch, _raise)
    runner = CliRunner()
    result = runner.invoke(main_app, ["run", "--config", str(cfg)])
    assert result.exit_code == 78
    assert "AuthError" in result.output


def test_lifecycle_run_config_error_exits_78(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path)

    async def _raise(_options: Any) -> None:
        raise ConfigError("bad config")

    _patch_boot(monkeypatch, _raise)
    runner = CliRunner()
    result = runner.invoke(main_app, ["run", "--config", str(cfg)])
    assert result.exit_code == 78
    assert "ConfigError" in result.output


def test_lifecycle_run_clean_exit_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: boot returns normally → exit code 0 (no error mapping)."""
    cfg = _write_config(tmp_path)

    async def _ok(_options: Any) -> None:
        return None

    _patch_boot(monkeypatch, _ok)
    runner = CliRunner()
    result = runner.invoke(main_app, ["run", "--config", str(cfg)])
    assert result.exit_code == 0, result.output


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.delenv("DAEYEON_BOT_CONFIG", raising=False)
