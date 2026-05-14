"""Doctor pre-flight checks: state dir, disk, heartbeat, pause, db, token."""

from __future__ import annotations

import time
from pathlib import Path

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


async def test_report_ok_property_false_on_fail(fresh_state_dir: Path) -> None:
    flag = fresh_state_dir / "heartbeat"
    flag.touch()
    import os

    very_old = time.time() - 60 * 60
    os.utime(flag, (very_old, very_old))
    report = await run_checks(_config(fresh_state_dir))
    assert report.ok is False
