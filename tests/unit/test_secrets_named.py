"""Named-secret loading on each provider (feature 002 secrets keys).

Mirrors `tests/unit/test_secrets.py` but exercises `load_secret(key)`
which was added so the daemon can resolve `JIRA_USER`, `JIRA_API_TOKEN`,
`SSW_AUTOMATION_PASSWORD`, etc. through the same provider chain.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from daeyeon_bot.core.errors import AuthError, ConfigError
from daeyeon_bot.infra.secrets import EnvSecrets, FileSecrets, KeychainSecrets

# ─── KeychainSecrets.load_secret ──────────────────────────────────────────────


def test_keychain_load_secret_returns_value() -> None:
    """`load_secret(key)` resolves `(service, account=key)`."""
    provider = KeychainSecrets(service="daeyeon-bot", account="oauth_token")
    with patch("keyring.get_password", return_value="atok-123") as gp:
        value = provider.load_secret("jira_api_token")
    assert value == "atok-123"
    gp.assert_called_once_with("daeyeon-bot", "jira_api_token")


def test_keychain_load_secret_raises_when_missing() -> None:
    provider = KeychainSecrets(service="daeyeon-bot", account="oauth_token")
    with patch("keyring.get_password", return_value=None):
        with pytest.raises(AuthError, match="no secret"):
            provider.load_secret("missing_key")


def test_keychain_load_secret_rejects_empty_key() -> None:
    provider = KeychainSecrets(service="daeyeon-bot", account="oauth_token")
    with pytest.raises(ConfigError, match="empty secret key"):
        provider.load_secret("")


# ─── FileSecrets.load_secret ──────────────────────────────────────────────────


def _write_0600(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def test_file_load_secret_reads_sibling_file(tmp_path: Path) -> None:
    oauth_path = tmp_path / "oauth_token"
    _write_0600(oauth_path, "oauth-value\n")
    _write_0600(tmp_path / "jira_api_token", "atok-xyz\n")
    provider = FileSecrets(path=oauth_path)
    assert provider.load_secret("jira_api_token") == "atok-xyz"


def test_file_load_secret_raises_when_missing(tmp_path: Path) -> None:
    oauth_path = tmp_path / "oauth_token"
    _write_0600(oauth_path, "oauth\n")
    provider = FileSecrets(path=oauth_path)
    with pytest.raises(AuthError, match="missing"):
        provider.load_secret("jira_api_token")


def test_file_load_secret_refuses_loose_perms(tmp_path: Path) -> None:
    oauth_path = tmp_path / "oauth_token"
    _write_0600(oauth_path, "oauth\n")
    bad = tmp_path / "jira_api_token"
    bad.write_text("atok\n", encoding="utf-8")
    bad.chmod(0o644)  # world-readable
    provider = FileSecrets(path=oauth_path)
    with pytest.raises(ConfigError, match="expected 0o600"):
        provider.load_secret("jira_api_token")


def test_file_load_secret_rejects_path_traversal(tmp_path: Path) -> None:
    oauth_path = tmp_path / "oauth_token"
    _write_0600(oauth_path, "oauth\n")
    provider = FileSecrets(path=oauth_path)
    with pytest.raises(ConfigError, match="invalid key"):
        provider.load_secret("../escape")
    with pytest.raises(ConfigError, match="invalid key"):
        provider.load_secret("sub/dir/key")


# ─── EnvSecrets.load_secret ──────────────────────────────────────────────────


def test_env_load_secret_uppercases_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_API_TOKEN", "atok-abc")
    provider = EnvSecrets()
    assert provider.load_secret("jira_api_token") == "atok-abc"


def test_env_load_secret_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    provider = EnvSecrets()
    with pytest.raises(AuthError, match="JIRA_API_TOKEN not set"):
        provider.load_secret("jira_api_token")


def test_env_load_secret_rejects_empty_key() -> None:
    provider = EnvSecrets()
    with pytest.raises(ConfigError, match="empty secret key"):
        provider.load_secret("")


# ─── load_oauth_token still works (backwards compat) ──────────────────────────


def test_keychain_oauth_token_path_unchanged() -> None:
    provider = KeychainSecrets(service="daeyeon-bot", account="oauth_token")
    with patch("keyring.get_password", return_value="tok-oauth"):
        assert provider.load_oauth_token() == "tok-oauth"


def test_file_oauth_token_path_unchanged(tmp_path: Path) -> None:
    oauth_path = tmp_path / "oauth_token"
    _write_0600(oauth_path, "oauth-value\n")
    provider = FileSecrets(path=oauth_path)
    assert provider.load_oauth_token() == "oauth-value"


def test_env_oauth_token_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-env")
    provider = EnvSecrets()
    assert provider.load_oauth_token() == "oauth-env"
