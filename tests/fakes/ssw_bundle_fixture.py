"""Tmp_path git fixture mimicking ssw-bundle.

Builds a tiny super-repo (with one bundled submodule) under `tmp_path`,
exposes its on-disk URL as `origin`, and returns the super-repo path.
Tests instantiate `SswBundleClient` against this fixture without any
network access.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(cwd),
        },
    )
    return proc.stdout


@dataclass(frozen=True, slots=True)
class SswBundleFixture:
    """One on-disk ssw-bundle simulacrum.

    Minimal — no submodule. The production code's `git submodule update
    --init --recursive --depth 1` is a no-op when `.gitmodules` is
    absent, so we exercise that path without the headaches of a fake
    submodule remote. End-to-end submodule scenarios are reserved for
    `tests/integration/`.
    """

    bundle_path: Path  # super-repo working copy
    bundle_remote_url: str  # `file://...` (actually `/path/to/bare.git`)
    main_commit: str


def build_fixture(tmp_path: Path) -> SswBundleFixture:
    """Build a minimal ssw-bundle fixture under `tmp_path`.

    Layout (after this returns):
      tmp_path/
        super/               — working super-repo
          test/system/suites/01__app/TC-0033-fixture.robot
        super.git/           — bare super-repo remote
    """
    super_work_dir = tmp_path / "super"
    super_remote_dir = tmp_path / "super.git"

    # Bare super-repo remote.
    super_remote_dir.mkdir()
    _git(super_remote_dir, "init", "--bare", "-b", "release/v3.2")

    # Working copy: populate, commit, push.
    super_work_dir.mkdir()
    _git(super_work_dir, "init", "-b", "release/v3.2")
    suite_dir = super_work_dir / "test" / "system" / "suites" / "01__app"
    suite_dir.mkdir(parents=True)
    (suite_dir / "TC-0033-fixture.robot").write_text(
        "*** Test Cases ***\nTC-0033-Dram_test_with_exception\n    Log    fixture\n",
        encoding="utf-8",
    )
    _git(super_work_dir, "add", ".")
    _git(super_work_dir, "commit", "-m", "init super-repo")
    _git(super_work_dir, "remote", "add", "origin", str(super_remote_dir))
    _git(super_work_dir, "push", "origin", "release/v3.2")

    main_commit = _git(super_work_dir, "rev-parse", "HEAD").strip()
    return SswBundleFixture(
        bundle_path=super_work_dir,
        bundle_remote_url=str(super_remote_dir),
        main_commit=main_commit,
    )


__all__ = ["SswBundleFixture", "build_fixture"]
