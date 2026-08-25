"""Feature 004 — the daemon's only write-side git adapter.

Two halves:
  * path guards, which are pure and cheap;
  * real git operations against a local bare repo in `tmp_path`. Those are real
    `git` subprocesses — no network, no GitHub — because the value of this
    module is entirely in whether the git invocations are correct, and a mocked
    subprocess would test the mock.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from daeyeon_bot.core.errors import ConfigError, PermanentError
from daeyeon_bot.infra.git_workspace import GitWorkspace, workspace_dir_name

pytestmark = pytest.mark.anyio if False else []


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    ).stdout


# ── path guards ───────────────────────────────────────────────────────────


def test_dir_name_flattens_owner_and_repo() -> None:
    assert workspace_dir_name("owner/repo") == "owner__repo"


def test_malformed_repo_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="malformed repo"):
        GitWorkspace(repo="no-slash", workspace_root=tmp_path / "ws")


def test_workspace_root_directly_in_home_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~/ssw-bundle`-style mistakes are how a bot ends up force-resetting the
    operator's own working tree. `infra/ssw_bundle.py` learned this first."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="HOME"):
        GitWorkspace(repo="o/r", workspace_root=tmp_path / "checkouts")


def test_workspace_root_outside_project_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="outside project root"):
        GitWorkspace(
            repo="o/r",
            workspace_root=tmp_path / "elsewhere" / "ws",
            project_root=tmp_path / "project",
        )


def test_allow_external_opts_out_of_the_project_root_guard(tmp_path: Path) -> None:
    ws = GitWorkspace(
        repo="o/r",
        workspace_root=tmp_path / "elsewhere" / "ws",
        project_root=tmp_path / "project",
        allow_external=True,
    )
    assert ws.path.name == "o__r"


def test_clone_path_stays_under_the_root(tmp_path: Path) -> None:
    ws = GitWorkspace(repo="owner/repo", workspace_root=tmp_path / "project" / "ws")
    assert ws.path == (tmp_path / "project" / "ws" / "owner__repo").resolve()


def test_remote_url_is_https(tmp_path: Path) -> None:
    ws = GitWorkspace(repo="owner/repo", workspace_root=tmp_path / "ws")
    assert ws.remote_url == "https://github.com/owner/repo.git"


# ── argument validation on the write path ─────────────────────────────────


async def test_checkout_rejects_a_malformed_sha(tmp_path: Path) -> None:
    ws = GitWorkspace(repo="o/r", workspace_root=tmp_path / "ws")
    with pytest.raises(PermanentError, match="malformed head_sha"):
        await ws.checkout_pr(pr_number=1, head_sha="not-a-sha")


@pytest.mark.parametrize("bad", ["../evil", ".../x", "-flag/repo", "no-slash", "a/b/c"])
async def test_push_rejects_a_malformed_head_repo(tmp_path: Path, bad: str) -> None:
    """`../evil` is the one that matters: a permissive `owner/repo` pattern turns
    it into the push URL `https://github.com/../evil.git`."""
    ws = GitWorkspace(repo="o/r", workspace_root=tmp_path / "ws")
    with pytest.raises(PermanentError, match="malformed head_repo"):
        await ws.push(head_repo=bad, head_ref="main")


def test_traversal_repo_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="malformed repo"):
        GitWorkspace(repo="../evil", workspace_root=tmp_path / "ws")


async def test_missing_workspace_reports_the_workspace_not_a_missing_git(
    tmp_path: Path,
) -> None:
    """Both failures surface as FileNotFoundError from the subprocess call;
    reporting the wrong one sends the operator hunting for a binary that is
    already installed."""
    ws = GitWorkspace(repo="o/r", workspace_root=tmp_path / "ws")
    with pytest.raises(ConfigError, match="does not exist"):
        await ws.checkout_pr(pr_number=1, head_sha="a" * 40)


@pytest.mark.parametrize("ref", ["", "--force", "bad ref", "refs/heads/x;rm -rf /"])
async def test_push_rejects_a_malformed_head_ref(tmp_path: Path, ref: str) -> None:
    """A ref starting with `-` would be read by git as an option, and a ref with
    shell metacharacters signals a payload that has no business being here."""
    ws = GitWorkspace(repo="o/r", workspace_root=tmp_path / "ws")
    with pytest.raises(PermanentError, match="malformed head_ref"):
        await ws.push(head_repo="o/r", head_ref=ref)


# ── real git ──────────────────────────────────────────────────────────────


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repo with one commit on `main`, standing in for GitHub."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    _git(work, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def _local_workspace(tmp_path: Path, origin: Path) -> GitWorkspace:
    """A workspace whose `origin` is the local bare repo, not github.com."""
    ws = GitWorkspace(repo="owner/repo", workspace_root=tmp_path / "ws", allow_external=True)
    ws.workspace_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(ws.path)],
        check=True,
        capture_output=True,
    )
    return ws


async def test_diff_reports_tracked_edits(tmp_path: Path, origin: Path) -> None:
    ws = _local_workspace(tmp_path, origin)
    (ws.path / "app.py").write_text("x = 1\ny = 2\n")
    diff = await ws.diff()
    assert diff.changed_files == ("app.py",)
    assert diff.insertions == 1
    assert not diff.is_empty


async def test_diff_counts_untracked_files_too(tmp_path: Path, origin: Path) -> None:
    """`git diff HEAD` alone misses new files, and a fix that ADDS a module is
    exactly the case where the size gate must still apply."""
    ws = _local_workspace(tmp_path, origin)
    (ws.path / "new_module.py").write_text("a\nb\nc\n")
    diff = await ws.diff()
    assert diff.changed_files == ("new_module.py",)
    assert diff.insertions == 3


async def test_diff_is_empty_on_a_clean_tree(tmp_path: Path, origin: Path) -> None:
    ws = _local_workspace(tmp_path, origin)
    diff = await ws.diff()
    assert diff.is_empty
    assert diff.total_lines == 0


async def test_gitignored_files_do_not_enter_the_diff(tmp_path: Path, origin: Path) -> None:
    ws = _local_workspace(tmp_path, origin)
    (ws.path / ".gitignore").write_text("build/\n")
    (ws.path / "build").mkdir()
    (ws.path / "build" / "artifact.bin").write_text("junk\n")
    diff = await ws.diff()
    assert "build/artifact.bin" not in diff.changed_files


async def test_revert_working_tree_discards_everything(tmp_path: Path, origin: Path) -> None:
    ws = _local_workspace(tmp_path, origin)
    (ws.path / "app.py").write_text("mutated\n")
    (ws.path / "stray.py").write_text("stray\n")
    await ws.revert_working_tree()
    diff = await ws.diff()
    assert diff.is_empty
    assert not (ws.path / "stray.py").exists()


async def test_commit_all_stages_and_returns_the_sha(tmp_path: Path, origin: Path) -> None:
    ws = _local_workspace(tmp_path, origin)
    (ws.path / "app.py").write_text("x = 2\n")
    sha = await ws.commit_all("fix(review): tighten the bound")
    assert len(sha) == 40
    log = _git(ws.path, "log", "-1", "--pretty=%an <%ae>%n%s")
    assert "daeyeon-bot <daeyeon-bot@users.noreply.github.com>" in log
    assert "fix(review): tighten the bound" in log
    assert (await ws.diff()).is_empty


async def test_existing_clone_pointing_elsewhere_is_refused(tmp_path: Path, origin: Path) -> None:
    """Repointing someone else's checkout at a different repo is never the right
    recovery; deleting the directory is."""
    ws = _local_workspace(tmp_path, origin)
    other = GitWorkspace(repo="someone/else", workspace_root=tmp_path / "ws", allow_external=True)
    # Same flattened dir name is impossible, so borrow the populated one.
    object.__setattr__(other, "path", ws.path)
    with pytest.raises(ConfigError, match="refusing to repoint"):
        await other.ensure_clone()


async def test_verify_reports_a_failing_command_without_raising(
    tmp_path: Path, origin: Path
) -> None:
    ws = _local_workspace(tmp_path, origin)
    outcome = await ws.run_verify("echo boom && exit 3", timeout_s=30.0)
    assert not outcome.passed
    assert outcome.exit_code == 3
    assert "boom" in outcome.output_tail


async def test_verify_passes_on_exit_zero(tmp_path: Path, origin: Path) -> None:
    ws = _local_workspace(tmp_path, origin)
    outcome = await ws.run_verify("true", timeout_s=30.0)
    assert outcome.passed


async def test_verify_runs_inside_the_workspace(tmp_path: Path, origin: Path) -> None:
    ws = _local_workspace(tmp_path, origin)
    outcome = await ws.run_verify("pwd", timeout_s=30.0)
    assert str(ws.path) in outcome.output_tail


async def test_verify_timeout_is_a_failure_not_an_exception(tmp_path: Path, origin: Path) -> None:
    """A slow verify IS a failed verify, and the handler's 'don't push' branch
    is already the correct response — raising would take a different path."""
    ws = _local_workspace(tmp_path, origin)
    outcome = await ws.run_verify("sleep 5", timeout_s=0.2)
    assert not outcome.passed
    assert outcome.exit_code == 124
    assert "timed out" in outcome.output_tail


async def test_concurrent_verifies_do_not_block_the_loop(tmp_path: Path, origin: Path) -> None:
    """Everything here is subprocess-based; a sync `subprocess.run` would stall
    the daemon's whole event loop for the length of a test suite."""
    ws = _local_workspace(tmp_path, origin)
    results = await asyncio.gather(
        ws.run_verify("sleep 0.3 && true", timeout_s=30.0),
        ws.run_verify("sleep 0.3 && true", timeout_s=30.0),
    )
    assert all(r.passed for r in results)
