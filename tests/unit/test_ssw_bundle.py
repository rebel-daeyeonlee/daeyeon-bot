"""SswBundleClient — T029 tests against a real-git tmp_path fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from daeyeon_bot.core.errors import ConfigError, PermanentError
from daeyeon_bot.infra.ssw_bundle import (
    SswBundleClient,
    UnresolvableCommitError,
)
from tests.fakes.ssw_bundle_fixture import build_fixture

# ── Path guards ──────────────────────────────────────────────────────────────


def test_constructor_refuses_operator_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~/ssw-bundle` is hard-banned regardless of allow_external."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / "ssw-bundle").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with pytest.raises(ConfigError, match="operator working tree"):
        SswBundleClient(
            clone_path=fake_home / "ssw-bundle",
            project_root=tmp_path,
            allow_external=True,
        )


def test_constructor_refuses_path_outside_project_root(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "clone"
    outside.parent.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    with pytest.raises(ConfigError, match="outside project root"):
        SswBundleClient(
            clone_path=outside,
            project_root=project_root,
            allow_external=False,
        )


def test_constructor_allows_outside_when_flag_set(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "clone"
    outside.parent.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    # No raise — just constructs.
    SswBundleClient(
        clone_path=outside,
        project_root=project_root,
        allow_external=True,
    )


def test_constructor_refuses_clone_with_wrong_origin(tmp_path: Path) -> None:
    """An existing .git/config with a different origin → ConfigError."""
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:somebody/else.git\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"remote\.origin\.url"):
        SswBundleClient(
            clone_path=clone,
            project_root=tmp_path,
            remote_url="git@github.com:rebellions-sw/ssw-bundle.git",
        )


# ── ensure_clone / ensure_checkout / submodule init (live git ops) ───────────


@pytest.mark.asyncio
async def test_ensure_clone_creates_repo(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    clone = tmp_path / "project" / "var" / "ssw-bundle"
    client = SswBundleClient(
        clone_path=clone,
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    await client.ensure_clone()
    assert (clone / ".git").exists()


@pytest.mark.asyncio
async def test_ensure_clone_is_idempotent(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    clone = tmp_path / "project" / "var" / "ssw-bundle"
    client = SswBundleClient(
        clone_path=clone,
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    await client.ensure_clone()
    await client.ensure_clone()  # second call does nothing


@pytest.mark.asyncio
async def test_ensure_checkout_valid_sha_succeeds(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    clone = tmp_path / "project" / "var" / "ssw-bundle"
    client = SswBundleClient(
        clone_path=clone,
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    await client.ensure_checkout(branch="release/v3.2", commit_sha=fixture.main_commit)
    # The TC file should be present.
    found = client.grep_test_case(tc_name="TC-0033-Dram_test_with_exception")
    assert found is not None


@pytest.mark.asyncio
async def test_ensure_checkout_invalid_sha_raises(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    client = SswBundleClient(
        clone_path=tmp_path / "project" / "var" / "ssw-bundle",
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    with pytest.raises(PermanentError, match="invalid commit sha"):
        await client.ensure_checkout(branch="release/v3.2", commit_sha="not-hex")


@pytest.mark.asyncio
async def test_ensure_checkout_unreachable_sha_raises(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    client = SswBundleClient(
        clone_path=tmp_path / "project" / "var" / "ssw-bundle",
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    unreachable = "0" * 40
    with pytest.raises(UnresolvableCommitError):
        await client.ensure_checkout(branch="release/v3.2", commit_sha=unreachable)


# ── read_file / grep_test_case ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_file_inside_clone_returns_contents(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    client = SswBundleClient(
        clone_path=tmp_path / "project" / "var" / "ssw-bundle",
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    await client.ensure_checkout(branch="release/v3.2", commit_sha=fixture.main_commit)
    contents = client.read_file("test/system/suites/01__app/TC-0033-fixture.robot")
    assert contents is not None
    assert "TC-0033-Dram_test_with_exception" in contents


@pytest.mark.asyncio
async def test_read_file_missing_returns_none(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    client = SswBundleClient(
        clone_path=tmp_path / "project" / "var" / "ssw-bundle",
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    await client.ensure_clone()
    assert client.read_file("does/not/exist.txt") is None


@pytest.mark.asyncio
async def test_read_file_path_traversal_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    client = SswBundleClient(
        clone_path=tmp_path / "project" / "var" / "ssw-bundle",
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    await client.ensure_clone()
    with pytest.raises(PermanentError, match="outside clone"):
        client.read_file("../../../etc/passwd")


@pytest.mark.asyncio
async def test_grep_test_case_finds_existing_tc(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    client = SswBundleClient(
        clone_path=tmp_path / "project" / "var" / "ssw-bundle",
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    await client.ensure_checkout(branch="release/v3.2", commit_sha=fixture.main_commit)
    found = client.grep_test_case(tc_name="TC-0033-Dram_test_with_exception")
    assert found is not None
    assert str(found).endswith("TC-0033-fixture.robot")


@pytest.mark.asyncio
async def test_grep_test_case_returns_none_for_unknown(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    client = SswBundleClient(
        clone_path=tmp_path / "project" / "var" / "ssw-bundle",
        remote_url=fixture.bundle_remote_url,
        project_root=tmp_path / "project",
    )
    await client.ensure_checkout(branch="release/v3.2", commit_sha=fixture.main_commit)
    assert client.grep_test_case(tc_name="TC-9999-nothing") is None


# ── Failed-submodule parsing (helper) ────────────────────────────────────────


def test_parse_failed_submodules_extracts_paths() -> None:
    # Private helper — accessed via module attribute for test only.
    from daeyeon_bot.infra import ssw_bundle as _bundle

    text = (
        "fatal: clone of 'git@x:y.git' into submodule path '/clone/products/atom/fw' failed\n"
        "Failed to clone 'products/common/kmd'. Retry?\n"
    )
    parse = _bundle._parse_failed_submodules  # pyright: ignore[reportPrivateUsage]
    out = parse(text)
    assert "/clone/products/atom/fw" in out
    assert "products/common/kmd" in out


# Note: testing actual submodule init failure end-to-end requires a fixture
# where one submodule remote is unreachable. We cover it via the helper above;
# the full integration scenario is reserved for tests/integration/.
