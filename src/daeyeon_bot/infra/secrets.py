"""Secret provider abstraction (PLAN.md §5 Phase 4 / CONTRACTS §7).

Provider order is decided by the operator via config:
    - keychain (macOS): uses `keyring` against a (service, account) pair.
    - file:           0o600 file on disk; we refuse looser permissions.
    - env:            `CLAUDE_CODE_OAUTH_TOKEN` env var, only with the
                      `--insecure-env` flag because the token then shows
                      up in /proc/<pid>/environ on Linux.

The token is held only in memory. After construction, providers do not
expose a way to round-trip the secret out to anything except the SDK.

In addition to the daemon's OAuth token, named secrets (e.g. `jira_user`,
`jira_api_token`, `ssw_automation_password` — see feature 002) are loaded
via `load_secret(key)` on the same provider. Lookup conventions:

| Provider | OAuth token | Named secret (key=`<snake>`) |
|---|---|---|
| keychain | (service, account="oauth_token") | (service, account=key) |
| file     | `file_path` | `file_path.parent / key` (also 0o600) |
| env      | `CLAUDE_CODE_OAUTH_TOKEN` | env var `key.upper()` (e.g. `JIRA_USER`) |
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import keyring
import keyring.errors

from daeyeon_bot.core.errors import AuthError, ConfigError


@runtime_checkable
class SecretsProvider(Protocol):
    """The minimal contract a secrets backend must satisfy."""

    def load_oauth_token(self) -> str: ...
    def load_secret(self, key: str) -> str: ...


@dataclass(frozen=True, slots=True)
class KeychainSecrets:
    """macOS Keychain (login keychain) lookup via `keyring`."""

    service: str
    account: str

    def load_oauth_token(self) -> str:
        return self._lookup(self.account)

    def load_secret(self, key: str) -> str:
        """Look up `(self.service, key)` in keychain."""
        if not key:
            raise ConfigError("keychain: empty secret key")
        return self._lookup(key)

    def _lookup(self, account: str) -> str:
        try:
            token = keyring.get_password(self.service, account)
        except keyring.errors.KeyringError as exc:
            # No backend on Linux without secretstorage/kwallet, locked keyring,
            # or any other backend-side failure. Surface as AuthError so the
            # doctor reports `fail` cleanly instead of crashing on boot.
            raise AuthError(f"keychain: backend unavailable ({exc})") from exc
        if not token:
            raise AuthError(f"keychain: no secret for service={self.service!r} account={account!r}")
        return token


@dataclass(frozen=True, slots=True)
class FileSecrets:
    """0o600 file on disk. Refuses to load if perms are looser.

    Named secrets live as sibling files next to the OAuth token file.
    Example: `path=/etc/daeyeon-bot/oauth_token`, then
    `load_secret("jira_api_token")` reads `/etc/daeyeon-bot/jira_api_token`.
    """

    path: Path

    def load_oauth_token(self) -> str:
        return self._read_token(self.path)

    def load_secret(self, key: str) -> str:
        """Read `<self.path.parent>/<key>` as a 0o600 file."""
        if not key:
            raise ConfigError("file secrets: empty secret key")
        # Guard against path-traversal in key (slashes, dotdot).
        if "/" in key or ".." in key or key.startswith("."):
            raise ConfigError(f"file secrets: invalid key {key!r}")
        return self._read_token(self.path.parent / key)

    @staticmethod
    def _read_token(path: Path) -> str:
        if not path.exists():
            raise AuthError(f"file secrets: missing {path}")
        st = path.stat()
        if _is_world_or_group_readable(st.st_mode):
            raise ConfigError(
                f"file secrets: {path} has perms {oct(st.st_mode & 0o777)};"
                " expected 0o600 (chmod 600 the file)"
            )
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise AuthError(f"file secrets: {path} is empty")
        return token


@dataclass(frozen=True, slots=True)
class EnvSecrets:
    """Reads CLAUDE_CODE_OAUTH_TOKEN from env. Insecure on Linux (/proc visible).

    Named secrets follow the env-var convention `KEY.upper()` —
    `load_secret("jira_user")` reads `os.environ["JIRA_USER"]`.
    """

    var_name: str = "CLAUDE_CODE_OAUTH_TOKEN"

    def load_oauth_token(self) -> str:
        token = os.environ.get(self.var_name)
        if not token:
            raise AuthError(f"env secrets: {self.var_name} not set")
        return token

    def load_secret(self, key: str) -> str:
        if not key:
            raise ConfigError("env secrets: empty secret key")
        env_name = key.upper()
        token = os.environ.get(env_name)
        if not token:
            raise AuthError(f"env secrets: {env_name} not set")
        return token


def build_provider(
    *,
    name: str,
    keychain_service: str,
    keychain_account: str,
    file_path: str,
    insecure_env_allowed: bool = False,
) -> SecretsProvider:
    """Construct a provider per the named strategy.

    `env` requires `insecure_env_allowed=True`; otherwise we raise
    ConfigError so the operator opts in explicitly.
    """
    if name == "keychain":
        return KeychainSecrets(service=keychain_service, account=keychain_account)
    if name == "file":
        return FileSecrets(path=Path(file_path).expanduser())
    if name == "env":
        if not insecure_env_allowed:
            raise ConfigError(
                "secrets.provider='env' requires --insecure-env (token would be visible"
                " in /proc/<pid>/environ on Linux)"
            )
        return EnvSecrets()
    raise ConfigError(f"unknown secrets provider: {name!r} (expected keychain|file|env)")


def _is_world_or_group_readable(mode: int) -> bool:
    """True if the file is readable by group or world (any of g/o read bits set)."""
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH))


__all__ = [
    "EnvSecrets",
    "FileSecrets",
    "KeychainSecrets",
    "SecretsProvider",
    "build_provider",
]
