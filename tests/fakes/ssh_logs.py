"""In-memory `SshLogClient` substitute.

Backed by `{(host, remote_path): {filename: bytes}}`. The handler /
integration tests inject this into the container without ever opening a
real SSH connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from daeyeon_bot.infra.ssh_logs import SshArtifact, SshFetchResult, SshSkip


@dataclass(slots=True)
class FakeSshLogClient:
    """Test double for `SshLogClient.fetch_directory`."""

    # `{(host, remote_path): {filename: bytes}}`
    files: dict[tuple[str, str], dict[str, bytes]] = field(default_factory=dict)
    # `{(host, remote_path): error_label}` — short-circuits the listdir.
    errors: dict[tuple[str, str], str] = field(default_factory=dict)
    # Per-host max-file override (else max_file_bytes from constructor).
    max_file_bytes: int = 10_485_760
    calls: list[tuple[str, str, list[str]]] = field(default_factory=list)

    def add_file(
        self,
        *,
        host: str,
        remote_path: str,
        filename: str,
        contents: bytes,
    ) -> None:
        self.files.setdefault((host, remote_path), {})[filename] = contents

    def set_error(self, *, host: str, remote_path: str, error: str) -> None:
        self.errors[(host, remote_path)] = error

    async def fetch_directory(
        self,
        *,
        host: str,
        remote_path: str,
        globs: list[str],
    ) -> SshFetchResult:
        self.calls.append((host, remote_path, list(globs)))
        key = (host, remote_path)
        if key in self.errors:
            return SshFetchResult(error=self.errors[key])
        entries = self.files.get(key, {})
        if not entries:
            return SshFetchResult(error=f"path_not_found:{remote_path}")
        artifacts: list[SshArtifact] = []
        skipped: list[SshSkip] = []
        for name in globs:
            data = entries.get(name)
            if data is None:
                skipped.append(SshSkip(filename=name, reason="not_found"))
                continue
            if len(data) > self.max_file_bytes:
                skipped.append(
                    SshSkip(
                        filename=name,
                        reason="oversized",
                        detail=f"{len(data)}>{self.max_file_bytes}",
                    )
                )
                continue
            artifacts.append(
                SshArtifact(
                    filename=name,
                    size_bytes=len(data),
                    contents=data.decode("utf-8", errors="replace"),
                )
            )
        return SshFetchResult(artifacts=tuple(artifacts), skipped=tuple(skipped))


__all__ = ["FakeSshLogClient"]
