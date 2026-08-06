"""Pre-flight checks: token / DB / config / migrations / disk / heartbeat.

The CLI (`daeyeon-bot ops doctor`) calls `run_checks(config)` and renders the
report. Every check returns a `CheckResult` with a status (`ok` / `warn` /
`fail`) and a one-line detail. Failures don't prevent later checks from
running — the operator wants the full picture in one shot.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite

from daeyeon_bot.app.config import Config
from daeyeon_bot.app.heartbeat import DEFAULT_TICK_S, staleness_seconds
from daeyeon_bot.core.errors import AuthError, ConfigError
from daeyeon_bot.infra import secrets, storage

CheckStatus = Literal["ok", "warn", "fail"]
DISK_WARN_BYTES = 100 * 1024 * 1024  # 100 MiB
DISK_FAIL_BYTES = 10 * 1024 * 1024  # 10 MiB

# One real Claude round trip. Generous because a cold CLI start on a fresh HOME
# does more work than a warm one.
AUTH_PROBE_TIMEOUT_S = 90.0
_AUTH_PROBE_NAME = "auth_probe"
_AUTH_PROBE_PROMPT = "Reply with exactly: DAEYEON_BOT_AUTH_OK"
_AUTH_PROBE_EXPECT = "DAEYEON_BOT_AUTH_OK"
# Substrings that mean "the credential was rejected" rather than "something else
# went wrong". Anything unrecognized is reported `warn`, not `fail` — a doctor
# that cries wolf on a network blip trains the operator to ignore it.
_AUTH_PROBE_FAIL_HINTS: tuple[str, ...] = (
    "failed to authenticate",
    "revoked",
    "unauthorized",
    "authentication_error",
    "invalid api key",
    "invalid_api_key",
    "401",
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.status != "fail" for r in self.results)


async def run_checks(config: Config, *, probe_auth: bool = False) -> DoctorReport:
    """Execute the full check suite. Order: cheap → expensive.

    `probe_auth` appends `auth_probe`, which spends one real Claude call. It is
    opt-in so the default suite stays offline, hermetic, and free; the automatic
    protection against a dead credential is `infra/claude.py:_raise_if_api_error`
    (`AuthError` → exit 78), not this check.
    """
    results: list[CheckResult] = [
        _check_state_dir(config.state_dir_path),
        _check_disk(config.state_dir_path),
        _check_heartbeat(config.state_dir_path / "heartbeat"),
        _check_pause_flag(config.pause_flag_path),
        await _check_db_and_migrations(config.db_path),
        _check_token(config),
    ]
    if probe_auth:
        results.append(await _check_auth_probe(config))
    return DoctorReport(results=tuple(results))


def _check_state_dir(state_dir: Path) -> CheckResult:
    name = "state_dir"
    if not state_dir.exists():
        return CheckResult(name=name, status="warn", detail=f"missing: {state_dir}")
    if not state_dir.is_dir():
        return CheckResult(name=name, status="fail", detail=f"not a directory: {state_dir}")
    return CheckResult(name=name, status="ok", detail=str(state_dir))


def _check_disk(state_dir: Path) -> CheckResult:
    name = "disk"
    target = state_dir if state_dir.exists() else state_dir.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return CheckResult(name=name, status="warn", detail=f"unreadable: {exc}")
    free_mb = usage.free // (1024 * 1024)
    if usage.free < DISK_FAIL_BYTES:
        return CheckResult(name=name, status="fail", detail=f"only {free_mb} MiB free")
    if usage.free < DISK_WARN_BYTES:
        return CheckResult(name=name, status="warn", detail=f"low: {free_mb} MiB free")
    return CheckResult(name=name, status="ok", detail=f"{free_mb} MiB free")


def _check_heartbeat(flag_path: Path) -> CheckResult:
    name = "heartbeat"
    age = staleness_seconds(flag_path, now_ts=time.time())
    if age is None:
        return CheckResult(name=name, status="warn", detail="no heartbeat file (daemon offline?)")
    if age > DEFAULT_TICK_S * 3:
        return CheckResult(name=name, status="fail", detail=f"stale: {int(age)}s old")
    return CheckResult(name=name, status="ok", detail=f"fresh ({int(age)}s ago)")


def _check_pause_flag(flag_path: Path) -> CheckResult:
    name = "pause"
    if flag_path.exists():
        return CheckResult(name=name, status="warn", detail=f"PAUSE active: {flag_path}")
    return CheckResult(name=name, status="ok", detail="not paused")


async def _check_db_and_migrations(db_path: Path) -> CheckResult:
    name = "db"
    if not await asyncio.to_thread(db_path.exists):
        return CheckResult(name=name, status="warn", detail=f"missing: {db_path} (run ops migrate)")
    try:
        async with storage.connection(db_path) as conn:
            integrity = await _integrity_check(conn)
            if integrity != "ok":
                return CheckResult(name=name, status="fail", detail=f"integrity: {integrity}")
            current = await _schema_version(conn)
            latest = _latest_migration_seq()
    except aiosqlite.Error as exc:
        return CheckResult(name=name, status="fail", detail=f"open failed: {exc}")
    if current < latest:
        return CheckResult(
            name=name,
            status="warn",
            detail=f"schema_version={current}, pending up to {latest} (run ops migrate)",
        )
    return CheckResult(name=name, status="ok", detail=f"schema_version={current}")


def _check_token(config: Config) -> CheckResult:
    """Probe the configured secrets provider and report success/failure.

    The token itself is never logged — only its length and the provider name.

    Readability only: this check cannot see a server-side revocation, so an `ok`
    here means "a secret exists", not "the daemon can call Claude". Use
    `--auth-probe` for the latter.
    """
    name = "token"
    try:
        token = _load_token(config)
    except ConfigError as exc:
        return CheckResult(name=name, status="fail", detail=f"config: {exc}")
    except AuthError as exc:
        return CheckResult(name=name, status="fail", detail=f"unavailable: {exc}")
    return CheckResult(
        name=name,
        status="ok",
        detail=f"provider={config.secrets.provider} (token len={len(token)})",
    )


def _load_token(config: Config) -> str:
    provider = secrets.build_provider(
        name=config.secrets.provider,
        keychain_service=config.secrets.keychain_service,
        keychain_account=config.secrets.keychain_account,
        file_path=config.secrets.file_path,
    )
    return provider.load_oauth_token()


async def _check_auth_probe(config: Config) -> CheckResult:
    """Spend one real Claude call to prove the *configured token* still works.

    **The isolated `HOME` is the whole point.** The `claude` CLI prefers
    `$HOME/.claude/.credentials.json` over `CLAUDE_CODE_OAUTH_TOKEN`, so a probe
    that inherits the operator's home validates their interactive login instead
    of the daemon's token — it returns a cheerful success while the configured
    secret is revoked. That shadowing is what hid a revoked token for two days
    on 2026-08-04 (verified: a deliberately corrupt token still succeeded with
    the real `HOME`, and failed with `401 ... revoked` under a fresh one).

    The token reaches the child through its environment, which is visible in
    `/proc` for the child's lifetime — the same exposure the SDK already accepts
    when it spawns the CLI, and the reason this is a short-lived subprocess.
    """
    try:
        token = _load_token(config)
    except (AuthError, ConfigError) as exc:
        return CheckResult(name=_AUTH_PROBE_NAME, status="fail", detail=f"token unavailable: {exc}")

    cli = shutil.which("claude")
    if cli is None:
        return CheckResult(name=_AUTH_PROBE_NAME, status="warn", detail="claude CLI not on PATH")

    outcome = await _run_auth_probe(cli, token)
    if isinstance(outcome, CheckResult):
        return outcome
    return _classify_auth_probe(outcome)


async def _run_auth_probe(cli: str, token: str) -> str | CheckResult:
    """Run the CLI under a throwaway HOME. Returns its output, or a failed check."""
    with tempfile.TemporaryDirectory(prefix="daeyeon-bot-auth-probe-") as isolated_home:
        env = {
            "HOME": isolated_home,
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            # An auto-update mid-probe would muddy the verdict.
            "DISABLE_AUTOUPDATER": "1",
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                cli,
                "-p",
                _AUTH_PROBE_PROMPT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=isolated_home,
            )
        except OSError as exc:
            return CheckResult(name=_AUTH_PROBE_NAME, status="warn", detail=f"spawn failed: {exc}")
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=AUTH_PROBE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return CheckResult(
                name=_AUTH_PROBE_NAME,
                status="warn",
                detail=f"timed out after {int(AUTH_PROBE_TIMEOUT_S)}s",
            )
    return stdout.decode("utf-8", errors="replace").strip()


def _classify_auth_probe(output: str) -> CheckResult:
    if _AUTH_PROBE_EXPECT in output:
        return CheckResult(name=_AUTH_PROBE_NAME, status="ok", detail="token accepted upstream")
    lowered = output.lower()
    if any(hint in lowered for hint in _AUTH_PROBE_FAIL_HINTS):
        return CheckResult(name=_AUTH_PROBE_NAME, status="fail", detail=f"rejected: {output[:160]}")
    return CheckResult(
        name=_AUTH_PROBE_NAME,
        status="warn",
        detail=f"inconclusive: {output[:160] or '(no output)'}",
    )


async def _integrity_check(conn: aiosqlite.Connection) -> str:
    async with conn.execute("PRAGMA integrity_check") as cur:
        row = await cur.fetchone()
    if row is None:
        return "no result"
    return str(row[0])


async def _schema_version(conn: aiosqlite.Connection) -> int:
    try:
        async with conn.execute("SELECT value FROM meta WHERE key='schema_version'") as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(row["value"]) if row is not None else 0


def _latest_migration_seq() -> int:
    """Return the highest migration sequence number bundled in this build."""
    files = storage.migration_files()
    return files[-1][0] if files else 0


__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "run_checks",
]
