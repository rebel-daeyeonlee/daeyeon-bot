"""Phase 4 secrets providers: keychain / file / env."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from daeyeon_bot.core.errors import AuthError, ConfigError
from daeyeon_bot.infra import secrets


def test_keychain_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def _get_password(_service: str, _account: str) -> str | None:
        return "tok-abc"

    monkeypatch.setattr(secrets.keyring, "get_password", _get_password)
    provider = secrets.KeychainSecrets(service="svc", account="acct")
    assert provider.load_oauth_token() == "tok-abc"


def test_keychain_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _get_password(_service: str, _account: str) -> str | None:
        return None

    monkeypatch.setattr(secrets.keyring, "get_password", _get_password)
    provider = secrets.KeychainSecrets(service="svc", account="acct")
    with pytest.raises(AuthError, match="keychain: no secret"):
        provider.load_oauth_token()


def test_keychain_wraps_no_backend_as_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux without secretstorage/kwallet raises NoKeyringError; we map to AuthError."""

    def _raise(_service: str, _account: str) -> str | None:
        raise secrets.keyring.errors.NoKeyringError("no recommended backend")

    monkeypatch.setattr(secrets.keyring, "get_password", _raise)
    provider = secrets.KeychainSecrets(service="svc", account="acct")
    with pytest.raises(AuthError, match="keychain: backend unavailable"):
        provider.load_oauth_token()


def test_file_secrets_reads_token(tmp_path: Path) -> None:
    secret_file = tmp_path / "token"
    secret_file.write_text("tok-xyz\n", encoding="utf-8")
    secret_file.chmod(0o600)
    provider = secrets.FileSecrets(path=secret_file)
    assert provider.load_oauth_token() == "tok-xyz"


def test_file_secrets_missing_raises_auth_error(tmp_path: Path) -> None:
    provider = secrets.FileSecrets(path=tmp_path / "absent")
    with pytest.raises(AuthError, match="missing"):
        provider.load_oauth_token()


def test_file_secrets_empty_raises_auth_error(tmp_path: Path) -> None:
    secret_file = tmp_path / "token"
    secret_file.write_text("   \n", encoding="utf-8")
    secret_file.chmod(0o600)
    provider = secrets.FileSecrets(path=secret_file)
    with pytest.raises(AuthError, match="empty"):
        provider.load_oauth_token()


def test_file_secrets_rejects_group_readable(tmp_path: Path) -> None:
    secret_file = tmp_path / "token"
    secret_file.write_text("tok\n", encoding="utf-8")
    secret_file.chmod(0o640)
    provider = secrets.FileSecrets(path=secret_file)
    with pytest.raises(ConfigError, match="0o600"):
        provider.load_oauth_token()


def test_file_secrets_rejects_world_readable(tmp_path: Path) -> None:
    secret_file = tmp_path / "token"
    secret_file.write_text("tok\n", encoding="utf-8")
    secret_file.chmod(0o604)
    provider = secrets.FileSecrets(path=secret_file)
    with pytest.raises(ConfigError, match="0o600"):
        provider.load_oauth_token()


def test_env_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok")
    provider = secrets.EnvSecrets()
    assert provider.load_oauth_token() == "env-tok"


def test_env_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    provider = secrets.EnvSecrets()
    with pytest.raises(AuthError, match="not set"):
        provider.load_oauth_token()


def test_build_provider_keychain() -> None:
    provider = secrets.build_provider(
        name="keychain",
        keychain_service="svc",
        keychain_account="acct",
        file_path="",
    )
    assert isinstance(provider, secrets.KeychainSecrets)


def test_build_provider_file(tmp_path: Path) -> None:
    target = tmp_path / "tok"
    provider = secrets.build_provider(
        name="file",
        keychain_service="",
        keychain_account="",
        file_path=str(target),
    )
    assert isinstance(provider, secrets.FileSecrets)
    assert provider.path == target


def test_build_provider_env_requires_opt_in() -> None:
    with pytest.raises(ConfigError, match="--insecure-env"):
        secrets.build_provider(
            name="env",
            keychain_service="",
            keychain_account="",
            file_path="",
        )


def test_build_provider_env_allows_when_flag_set() -> None:
    provider = secrets.build_provider(
        name="env",
        keychain_service="",
        keychain_account="",
        file_path="",
        insecure_env_allowed=True,
    )
    assert isinstance(provider, secrets.EnvSecrets)


def test_build_provider_unknown_raises() -> None:
    with pytest.raises(ConfigError, match="unknown secrets provider"):
        secrets.build_provider(
            name="vault",
            keychain_service="",
            keychain_account="",
            file_path="",
        )


def test_world_or_group_helper_flags_loose_perms() -> None:
    assert secrets._is_world_or_group_readable(0o640) is True  # pyright: ignore[reportPrivateUsage]
    assert secrets._is_world_or_group_readable(0o604) is True  # pyright: ignore[reportPrivateUsage]
    assert secrets._is_world_or_group_readable(0o600) is False  # pyright: ignore[reportPrivateUsage]
    assert secrets._is_world_or_group_readable(stat.S_IRUSR | stat.S_IWUSR) is False  # pyright: ignore[reportPrivateUsage]


def test_file_secrets_expanduser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = home / "tok"
    target.write_text("tok-home", encoding="utf-8")
    target.chmod(0o600)

    provider = secrets.build_provider(
        name="file",
        keychain_service="",
        keychain_account="",
        file_path="~/tok",
    )
    assert isinstance(provider, secrets.FileSecrets)
    assert provider.load_oauth_token() == "tok-home"


def test_file_secrets_actually_loads_after_chmod(tmp_path: Path) -> None:
    secret_file = tmp_path / "tok"
    secret_file.write_text("tok-fs", encoding="utf-8")
    secret_file.chmod(0o600)
    provider = secrets.FileSecrets(path=secret_file)
    assert provider.load_oauth_token() == "tok-fs"
