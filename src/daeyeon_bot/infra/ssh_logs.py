"""SSH log-dump fetcher via `asyncssh`.

Used by the `jira_triage` handler to pull RF artifacts (output.xml,
dmesg.log, console.log) from
`ssh://automation@<host>:/mnt/data/logs/regression-test/<run-id>/<host>/<TC>/`.

Credentials are shared-lab: `automation` / `SSW_AUTOMATION_PASSWORD`
(both literal strings as of 2026-05-13). The literal password is
registered with the structlog redaction processor at boot via
`register_literal_secret()` (see `infra/logging.py`), so it never lands
in logs.

Known-hosts file mode is `accept-new` on first contact, strict
afterward. The file lives under `<state_dir>/jira_triage_known_hosts`
and is created with `0o600`.

Error policy: connection / auth / host-key / SFTP failures populate
`SshFetchResult.error` rather than raising — the handler treats an SSH
outage as partial-data, not a triage-killing failure.
"""

from __future__ import annotations

import stat as stat_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

_DEFAULT_CONNECT_TIMEOUT_S = 10
_KEEPALIVE_S = 30


@dataclass(frozen=True, slots=True)
class SshArtifact:
    """One successfully fetched file."""

    filename: str
    size_bytes: int
    contents: str


@dataclass(frozen=True, slots=True)
class SshSkip:
    """One file we chose not to fetch (oversized / not found)."""

    filename: str
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SshFetchResult:
    """All artifacts + skips + maybe a whole-fetch error label."""

    artifacts: tuple[SshArtifact, ...] = ()
    skipped: tuple[SshSkip, ...] = ()
    error: str | None = None  # populated when connect/auth/path failed


@dataclass(slots=True)
class SshLogClient:
    """SFTP-only log-dump reader. One instance per daemon."""

    username: str
    password: str
    known_hosts_path: Path
    max_file_bytes: int = 10_485_760
    connect_timeout_s: int = _DEFAULT_CONNECT_TIMEOUT_S
    # Test seam: caller can supply a fake `asyncssh.connect`.
    connect_fn: Any = field(default=asyncssh.connect, repr=False)

    def __post_init__(self) -> None:
        # Refuse looser perms on the known-hosts file if it already exists.
        if self.known_hosts_path.exists():
            mode = self.known_hosts_path.stat().st_mode & 0o777
            if mode & (stat_mod.S_IRGRP | stat_mod.S_IROTH | stat_mod.S_IWGRP | stat_mod.S_IWOTH):
                raise PermissionError(
                    f"ssh_logs: known_hosts file {self.known_hosts_path} has perms"
                    f" {oct(mode)}; expected 0o600"
                )
        else:
            # Create it so asyncssh can read it on first contact.
            self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
            self.known_hosts_path.touch(mode=0o600, exist_ok=False)

    async def fetch_directory(
        self,
        *,
        host: str,
        remote_path: str,
        globs: list[str],
    ) -> SshFetchResult:
        """SSH-connect, listdir, fetch matching files. All errors → result.error."""
        try:
            return await self._fetch(host=host, remote_path=remote_path, globs=globs)
        except asyncssh.PermissionDenied:
            return SshFetchResult(error="auth_failed")
        except asyncssh.HostKeyNotVerifiable:
            return SshFetchResult(error=f"host_key_changed:{host}")
        except TimeoutError:
            # Must precede OSError — TimeoutError is a subclass of OSError on POSIX.
            return SshFetchResult(error="connect_timeout")
        except (asyncssh.ConnectionLost, OSError) as exc:
            return SshFetchResult(error=f"connect_failed:{exc}")

    async def _fetch(
        self,
        *,
        host: str,
        remote_path: str,
        globs: list[str],
    ) -> SshFetchResult:
        async with self.connect_fn(  # type: ignore[misc]
            host=host,
            username=self.username,
            password=self.password,
            known_hosts=str(self.known_hosts_path),
            connect_timeout=self.connect_timeout_s,
            keepalive_interval=_KEEPALIVE_S,
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                try:
                    entries_raw = await sftp.listdir(remote_path)
                except asyncssh.sftp.SFTPNoSuchFile:
                    return SshFetchResult(error=f"path_not_found:{remote_path}")
                except asyncssh.sftp.SFTPError as exc:
                    return SshFetchResult(error=f"sftp:{exc.code}")

                entries = {str(e) for e in entries_raw if str(e) not in (".", "..")}
                artifacts: list[SshArtifact] = []
                skipped: list[SshSkip] = []
                for name in globs:
                    if name not in entries:
                        skipped.append(SshSkip(filename=name, reason="not_found"))
                        continue
                    remote_file = f"{remote_path.rstrip('/')}/{name}"
                    try:
                        attrs = await sftp.lstat(remote_file)
                    except asyncssh.sftp.SFTPNoSuchFile:
                        skipped.append(SshSkip(filename=name, reason="not_found"))
                        continue
                    size = int(getattr(attrs, "size", 0) or 0)
                    if size > self.max_file_bytes:
                        skipped.append(
                            SshSkip(
                                filename=name,
                                reason="oversized",
                                detail=f"{size}>{self.max_file_bytes}",
                            )
                        )
                        continue
                    try:
                        async with sftp.open(remote_file, "rb") as fp:
                            raw_bytes: bytes = await fp.read()
                    except asyncssh.sftp.SFTPError as exc:
                        skipped.append(
                            SshSkip(filename=name, reason="read_failed", detail=str(exc))
                        )
                        continue
                    contents = raw_bytes.decode("utf-8", errors="replace")
                    artifacts.append(SshArtifact(filename=name, size_bytes=size, contents=contents))
                return SshFetchResult(
                    artifacts=tuple(artifacts),
                    skipped=tuple(skipped),
                )


__all__ = [
    "SshArtifact",
    "SshFetchResult",
    "SshLogClient",
    "SshSkip",
]
