"""OncallWiki adapter (feature 003): path guard, known_hosts, git env, search."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from daeyeon_bot.core.errors import ConfigError
from daeyeon_bot.infra.oncall_wiki import (
    INCIDENTS_DIR,
    RECOVERY_PLAYBOOK_PATH,
    OncallWiki,
)


def _mk(
    clone: Path, *, project_root: Path | None = None, allow_external: bool = False
) -> OncallWiki:
    return OncallWiki(
        clone_path=clone,
        known_hosts_path=clone.parent / "kh",
        remote_url="git@github.com:rebellions-sw/ssw-devops-oncall.git",
        project_root=project_root,
        allow_external=allow_external,
    )


def test_hard_ban_operator_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="operator working vault"):
        _mk(tmp_path / "ssw-devops-oncall")


def test_outside_project_root_refused(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "clone"
    with pytest.raises(ConfigError, match="outside project root"):
        _mk(outside, project_root=root)


def test_known_hosts_created_0600(tmp_path: Path) -> None:
    w = _mk(tmp_path / "var" / "wiki", project_root=tmp_path)
    assert w.known_hosts_path.exists()
    mode = w.known_hosts_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_git_env_headless_safe(tmp_path: Path) -> None:
    w = _mk(tmp_path / "var" / "wiki", project_root=tmp_path)
    env = w._git_env()  # pyright: ignore[reportPrivateUsage]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=accept-new" in env["GIT_SSH_COMMAND"]
    assert str(w.known_hosts_path) in env["GIT_SSH_COMMAND"]


def test_is_healthy_clone_sentinel(tmp_path: Path) -> None:
    clone = tmp_path / "var" / "wiki"
    w = _mk(clone, project_root=tmp_path)
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()
    assert w._is_healthy_clone() is False  # no sentinel yet  # pyright: ignore[reportPrivateUsage]
    (clone / RECOVERY_PLAYBOOK_PATH).parent.mkdir(parents=True)
    (clone / RECOVERY_PLAYBOOK_PATH).write_text("# playbook\n", encoding="utf-8")
    assert w._is_healthy_clone() is True  # pyright: ignore[reportPrivateUsage]


def _seed_vault(clone: Path) -> None:
    inc = clone / INCIDENTS_DIR
    inc.mkdir(parents=True)
    (clone / RECOVERY_PLAYBOOK_PATH).parent.mkdir(parents=True, exist_ok=True)
    (clone / RECOVERY_PLAYBOOK_PATH).write_text("# Recovery Playbook\n", encoding="utf-8")
    (inc / "qemu-golden-base-image-missing.md").write_text(
        '---\nsignature: "VM creation failed (golden base image missing)"\n'
        "domain: DevOps\n---\n# qemu golden base\nrsync golden-base failed\n",
        encoding="utf-8",
    )
    (inc / "unrelated.md").write_text(
        '---\nsignature: "SMC FW update timeout"\n---\n# smc\nqemu appears once here\n',
        encoding="utf-8",
    )


async def test_search_signature_first_ranking(tmp_path: Path) -> None:
    clone = tmp_path / "var" / "wiki"
    clone.mkdir(parents=True)
    _seed_vault(clone)
    w = _mk(clone, project_root=tmp_path)
    matches = await w.search(signatures=("VM creation failed",), phrases=("qemu",))
    paths = [m.path for m in matches]
    # The signature-matched incident ranks first; recovery playbook is always present.
    assert paths[0] == f"{INCIDENTS_DIR}/qemu-golden-base-image-missing.md"
    assert matches[0].signature_matched is True
    assert RECOVERY_PLAYBOOK_PATH in paths


def _git(args: list[str], cwd: Path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def test_ensure_fresh_clone_and_corrupt_reclone(tmp_path: Path) -> None:
    # Build a local source repo to clone from (file:// remote — no SSH).
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q"], src)
    _seed_vault(src)
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "init"], src)

    clone = tmp_path / "var" / "wiki"
    w = OncallWiki(
        clone_path=clone,
        known_hosts_path=tmp_path / "kh",
        remote_url=f"file://{src}",
        project_root=tmp_path,
    )
    r1 = await w.ensure_fresh()
    assert r1.available is True
    assert (clone / RECOVERY_PLAYBOOK_PATH).is_file()

    # Corrupt the clone (empty worktree, .git present) → detected & re-cloned.
    (clone / RECOVERY_PLAYBOOK_PATH).unlink()
    assert w._is_healthy_clone() is False  # pyright: ignore[reportPrivateUsage]
    r2 = await w.ensure_fresh()
    assert r2.available is True
    assert (clone / RECOVERY_PLAYBOOK_PATH).is_file()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(test_ensure_fresh_clone_and_corrupt_reclone(Path("/tmp/x")))
