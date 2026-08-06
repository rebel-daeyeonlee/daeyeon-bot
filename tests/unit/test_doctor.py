"""Doctor pre-flight checks: state dir, disk, heartbeat, pause, db, token."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import cast

import pytest

from daeyeon_bot.app.config import (
    Config,
    HandlerEntry,
    LoggingSection,
    RetentionSection,
    RuntimeSection,
    SecretsSection,
)
from daeyeon_bot.app.doctor import CheckResult, DoctorReport, run_checks
from daeyeon_bot.core.errors import AuthError
from daeyeon_bot.infra import secrets, storage


def _config(state_dir: Path) -> Config:
    return Config(
        runtime=RuntimeSection(state_dir=str(state_dir)),
        logging=LoggingSection(),
        secrets=SecretsSection(provider="keychain"),
        retention=RetentionSection(),
        triggers={},
        handlers={"echo": HandlerEntry(enabled=True)},
        routing={},
    )


def _by_name(report: DoctorReport, name: str) -> CheckResult:
    return next(r for r in report.results if r.name == name)


@pytest.fixture
def fresh_state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    return state


async def test_run_checks_returns_all_named_checks(fresh_state_dir: Path) -> None:
    report = await run_checks(_config(fresh_state_dir))
    names = {r.name for r in report.results}
    assert names == {"state_dir", "disk", "heartbeat", "pause", "db", "token"}


async def test_state_dir_ok_when_exists(fresh_state_dir: Path) -> None:
    report = await run_checks(_config(fresh_state_dir))
    assert _by_name(report, "state_dir").status == "ok"


async def test_state_dir_warn_when_missing(tmp_path: Path) -> None:
    report = await run_checks(_config(tmp_path / "missing"))
    assert _by_name(report, "state_dir").status == "warn"


async def test_heartbeat_warn_when_missing(fresh_state_dir: Path) -> None:
    report = await run_checks(_config(fresh_state_dir))
    assert _by_name(report, "heartbeat").status == "warn"


async def test_heartbeat_ok_when_fresh(fresh_state_dir: Path) -> None:
    (fresh_state_dir / "heartbeat").touch()
    report = await run_checks(_config(fresh_state_dir))
    assert _by_name(report, "heartbeat").status == "ok"


async def test_heartbeat_fail_when_stale(fresh_state_dir: Path) -> None:
    flag = fresh_state_dir / "heartbeat"
    flag.touch()
    very_old = time.time() - 60 * 60  # 1h ago
    import os

    os.utime(flag, (very_old, very_old))
    report = await run_checks(_config(fresh_state_dir))
    assert _by_name(report, "heartbeat").status == "fail"


async def test_pause_ok_when_not_paused(fresh_state_dir: Path) -> None:
    report = await run_checks(_config(fresh_state_dir))
    assert _by_name(report, "pause").status == "ok"


async def test_pause_warn_when_active(fresh_state_dir: Path) -> None:
    (fresh_state_dir / "PAUSE").touch()
    report = await run_checks(_config(fresh_state_dir))
    assert _by_name(report, "pause").status == "warn"


async def test_db_warn_when_missing(fresh_state_dir: Path) -> None:
    report = await run_checks(_config(fresh_state_dir))
    assert _by_name(report, "db").status == "warn"


async def test_db_ok_when_migrated(fresh_state_dir: Path) -> None:
    db_path = fresh_state_dir / "state.db"
    conn = await storage.open_db(db_path)
    try:
        await storage.apply_migrations(conn)
    finally:
        await conn.close()

    report = await run_checks(_config(fresh_state_dir))
    db_result = _by_name(report, "db")
    assert db_result.status == "ok"
    assert "schema_version=" in db_result.detail


class _StubProvider:
    def load_oauth_token(self) -> str:
        return "stub-token-12345"

    def load_secret(self, key: str) -> str:
        return f"stub-secret-{key}"


async def test_token_check_ok_when_provider_returns_token(
    fresh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _build(**_: object) -> secrets.SecretsProvider:
        return _StubProvider()

    monkeypatch.setattr(secrets, "build_provider", _build)
    report = await run_checks(_config(fresh_state_dir))
    token = _by_name(report, "token")
    assert token.status == "ok"
    assert "provider=keychain" in token.detail
    assert "token len=16" in token.detail


async def test_token_check_fail_when_provider_unavailable(
    fresh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _build(**_: object) -> secrets.SecretsProvider:
        raise AuthError("keychain: no token")

    monkeypatch.setattr(secrets, "build_provider", _build)
    report = await run_checks(_config(fresh_state_dir))
    token = _by_name(report, "token")
    assert token.status == "fail"
    assert "unavailable" in token.detail


# ── auth_probe ─────────────────────────────────────────────────────────────
#
# `token` only proves the secret is readable. On 2026-08-04 a revoked token sat
# behind a green `token ✓` for two days because the `claude` CLI prefers
# `$HOME/.claude/.credentials.json` over `CLAUDE_CODE_OAUTH_TOKEN` — so the
# isolated HOME below is the load-bearing part of this check, not a detail.


class _FakeProc:
    def __init__(self, output: bytes) -> None:
        self._output = output
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._output, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return 0


def _which_present(_name: str) -> str | None:
    return "/usr/bin/claude"


def _which_missing(_name: str) -> str | None:
    return None


def _stub_cli_returning(
    output: bytes, captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pretend `claude` exists on PATH and answers with `output`."""

    def _build(**_: object) -> secrets.SecretsProvider:
        return _StubProvider()

    monkeypatch.setattr(secrets, "build_provider", _build)
    monkeypatch.setattr(shutil, "which", _which_present)

    async def _exec(*args: object, **kwargs: object) -> _FakeProc:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc(output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)


async def test_auth_probe_absent_unless_requested(fresh_state_dir: Path) -> None:
    report = await run_checks(_config(fresh_state_dir))
    assert all(r.name != "auth_probe" for r in report.results)


async def test_auth_probe_isolates_home_and_passes_the_configured_token(
    fresh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe must not be able to see the operator's interactive login.

    If `HOME` were inherited, the CLI would authenticate with
    `~/.claude/.credentials.json` and report success for a revoked token —
    verified empirically on 2026-08-06 with a deliberately corrupt token.
    """
    captured: dict[str, object] = {}
    _stub_cli_returning(b"DAEYEON_BOT_AUTH_OK\n", captured, monkeypatch)

    report = await run_checks(_config(fresh_state_dir), probe_auth=True)
    assert _by_name(report, "auth_probe").status == "ok"

    env = captured["env"]
    assert isinstance(env, dict)
    probe_env = cast("dict[str, str]", env)
    assert probe_env["CLAUDE_CODE_OAUTH_TOKEN"] == "stub-token-12345"
    assert probe_env["HOME"] != os.environ.get("HOME")
    assert "auth-probe" in probe_env["HOME"]
    # No inherited credential directory can exist under a fresh temp HOME.
    assert not (Path(probe_env["HOME"]) / ".claude").exists()


async def test_auth_probe_fails_on_revoked_token(
    fresh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    _stub_cli_returning(
        b"Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
        captured,
        monkeypatch,
    )
    report = await run_checks(_config(fresh_state_dir), probe_auth=True)
    probe = _by_name(report, "auth_probe")
    assert probe.status == "fail"
    assert "revoked" in probe.detail
    assert report.ok is False


async def test_auth_probe_warns_on_unrecognized_output(
    fresh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfamiliar failure is `warn`: a doctor that cries wolf gets ignored."""
    captured: dict[str, object] = {}
    _stub_cli_returning(b"connect ETIMEDOUT 1.2.3.4:443", captured, monkeypatch)
    report = await run_checks(_config(fresh_state_dir), probe_auth=True)
    probe = _by_name(report, "auth_probe")
    assert probe.status == "warn"
    assert "inconclusive" in probe.detail


async def test_auth_probe_warns_when_cli_missing(
    fresh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _build(**_: object) -> secrets.SecretsProvider:
        return _StubProvider()

    monkeypatch.setattr(secrets, "build_provider", _build)
    monkeypatch.setattr(shutil, "which", _which_missing)
    report = await run_checks(_config(fresh_state_dir), probe_auth=True)
    probe = _by_name(report, "auth_probe")
    assert probe.status == "warn"
    assert "not on PATH" in probe.detail


async def test_auth_probe_fails_without_spawning_when_token_unreadable(
    fresh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _build(**_: object) -> secrets.SecretsProvider:
        raise AuthError("keychain: no token")

    monkeypatch.setattr(secrets, "build_provider", _build)

    async def _never(*_args: object, **_kwargs: object) -> _FakeProc:  # pragma: no cover
        raise AssertionError("must not spawn the CLI without a token")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _never)
    report = await run_checks(_config(fresh_state_dir), probe_auth=True)
    assert _by_name(report, "auth_probe").status == "fail"


async def test_report_ok_property_false_on_fail(fresh_state_dir: Path) -> None:
    flag = fresh_state_dir / "heartbeat"
    flag.touch()
    import os

    very_old = time.time() - 60 * 60
    os.utime(flag, (very_old, very_old))
    report = await run_checks(_config(fresh_state_dir))
    assert report.ok is False
