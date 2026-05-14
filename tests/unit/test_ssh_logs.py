"""SshLogClient — T026 tests against a fake `asyncssh.connect`.

We don't spin up a real sshd; instead we inject a fake connect function
that yields a mock SSH/SFTP client. This covers the wrapper's error
mapping + listdir/lstat/read flow without network.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh
import pytest

from daeyeon_bot.infra.ssh_logs import SshLogClient

# ── Fake asyncssh harness ────────────────────────────────────────────────────


@dataclass
class _FakeAttrs:
    size: int


class _FakeSftpFile:
    def __init__(self, contents: bytes) -> None:
        self._contents = contents

    async def read(self) -> bytes:
        return self._contents

    async def __aenter__(self) -> _FakeSftpFile:
        return self

    async def __aexit__(self, *_args: Any) -> None: ...


class _FakeSftpClient:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    async def listdir(self, _path: str) -> list[str]:
        return list(self._files.keys())

    async def lstat(self, path: str) -> _FakeAttrs:
        name = path.rsplit("/", 1)[-1]
        if name not in self._files:
            raise asyncssh.sftp.SFTPNoSuchFile("no")
        return _FakeAttrs(size=len(self._files[name]))

    def open(self, path: str, _mode: str) -> _FakeSftpFile:
        name = path.rsplit("/", 1)[-1]
        return _FakeSftpFile(self._files[name])

    async def __aenter__(self) -> _FakeSftpClient:
        return self

    async def __aexit__(self, *_args: Any) -> None: ...


class _FakeSshConn:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def start_sftp_client(self) -> _FakeSftpClient:
        return _FakeSftpClient(self._files)

    async def __aenter__(self) -> _FakeSshConn:
        return self

    async def __aexit__(self, *_args: Any) -> None: ...


@dataclass
class _Scenario:
    files: dict[str, bytes] = field(default_factory=dict)
    listdir_error: Exception | None = None
    connect_error: Exception | None = None

    def make_connect(self) -> Any:
        scenario = self

        def _connect(**_kwargs: Any) -> Any:
            if scenario.connect_error is not None:
                raise scenario.connect_error

            @asynccontextmanager  # type: ignore[arg-type, misc]
            async def _ctx():  # type: ignore[no-untyped-def]
                conn = _FakeSshConn(scenario.files)
                if scenario.listdir_error is not None:
                    # Hot-swap listdir to raise.
                    orig = conn.start_sftp_client

                    def _start() -> Any:
                        sftp = orig()

                        async def _list(_p: str) -> list[str]:
                            raise scenario.listdir_error  # type: ignore[misc]

                        sftp.listdir = _list  # type: ignore[method-assign]
                        return sftp

                    conn.start_sftp_client = _start  # type: ignore[method-assign]
                yield conn

            return _ctx()

        return _connect


def _client(tmp_path: Path, scenario: _Scenario) -> SshLogClient:
    return SshLogClient(
        username="automation",
        password="automation",
        known_hosts_path=tmp_path / "known_hosts",
        max_file_bytes=1024,
        connect_fn=scenario.make_connect(),
    )


# ── Happy path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_directory_returns_artifacts(tmp_path: Path) -> None:
    scenario = _Scenario(files={"output.xml": b"<?xml?>", "dmesg.log": b"kern stuff"})
    client = _client(tmp_path, scenario)
    result = await client.fetch_directory(
        host="ssw-giga-02",
        remote_path="/mnt/data/logs/regression-test/x/y/z",
        globs=["output.xml", "dmesg.log"],
    )
    assert result.error is None
    assert {a.filename for a in result.artifacts} == {"output.xml", "dmesg.log"}
    assert result.skipped == ()


@pytest.mark.asyncio
async def test_fetch_directory_skips_missing_files(tmp_path: Path) -> None:
    scenario = _Scenario(files={"output.xml": b"x"})
    client = _client(tmp_path, scenario)
    result = await client.fetch_directory(
        host="h",
        remote_path="/p",
        globs=["output.xml", "missing.log"],
    )
    assert result.artifacts and result.artifacts[0].filename == "output.xml"
    assert {s.filename: s.reason for s in result.skipped} == {"missing.log": "not_found"}


@pytest.mark.asyncio
async def test_fetch_directory_skips_oversized(tmp_path: Path) -> None:
    scenario = _Scenario(files={"big.bin": b"x" * 2048})  # > 1024 cap
    client = _client(tmp_path, scenario)
    result = await client.fetch_directory(host="h", remote_path="/p", globs=["big.bin"])
    assert result.artifacts == ()
    assert any(s.reason == "oversized" for s in result.skipped)


# ── Error mapping ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_failed_returns_auth_failed_error(tmp_path: Path) -> None:
    scenario = _Scenario(connect_error=asyncssh.PermissionDenied("nope"))
    client = _client(tmp_path, scenario)
    result = await client.fetch_directory(host="h", remote_path="/p", globs=["x"])
    assert result.error == "auth_failed"


@pytest.mark.asyncio
async def test_host_key_changed(tmp_path: Path) -> None:
    scenario = _Scenario(connect_error=asyncssh.HostKeyNotVerifiable("changed"))
    client = _client(tmp_path, scenario)
    result = await client.fetch_directory(host="bad-host", remote_path="/p", globs=["x"])
    assert result.error == "host_key_changed:bad-host"


@pytest.mark.asyncio
async def test_connect_failed_via_oserror(tmp_path: Path) -> None:
    scenario = _Scenario(connect_error=OSError("network unreachable"))
    client = _client(tmp_path, scenario)
    result = await client.fetch_directory(host="h", remote_path="/p", globs=["x"])
    assert result.error is not None and result.error.startswith("connect_failed")


@pytest.mark.asyncio
async def test_path_not_found(tmp_path: Path) -> None:
    scenario = _Scenario(listdir_error=asyncssh.sftp.SFTPNoSuchFile("no"))
    client = _client(tmp_path, scenario)
    result = await client.fetch_directory(host="h", remote_path="/missing", globs=["x"])
    assert result.error == "path_not_found:/missing"


# ── known_hosts perms ────────────────────────────────────────────────────────


def test_constructor_refuses_loose_known_hosts(tmp_path: Path) -> None:
    kh = tmp_path / "known_hosts"
    kh.write_text("")
    kh.chmod(0o644)
    with pytest.raises(PermissionError, match="expected 0o600"):
        SshLogClient(
            username="automation",
            password="automation",
            known_hosts_path=kh,
        )


def test_constructor_creates_known_hosts_with_0600(tmp_path: Path) -> None:
    kh = tmp_path / "fresh_known_hosts"
    SshLogClient(
        username="automation",
        password="automation",
        known_hosts_path=kh,
    )
    assert kh.exists()
    mode = kh.stat().st_mode & 0o777
    assert mode == 0o600
