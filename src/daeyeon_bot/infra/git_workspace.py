"""Path-guarded git workspace for the `pr_autofix` handler (feature 004).

This is the FIRST write-side git adapter in the daemon. `infra/ssw_bundle.py`
states in its own docstring that it has "no `push`, `commit`, or arbitrary-command
escape hatch"; that stays true — this module is the separate, deliberately
narrow place where those verbs live.

Layout — one throwaway clone per repo under the workspace root:

    <workspace_root>/<owner>__<repo>/

Guards, all constructor- or call-time:
  * the resolved clone path must sit under `workspace_root`;
  * `workspace_root` must sit under `project_root` unless `allow_external=True`;
  * the root may never resolve to `$HOME` or `$HOME/<repo>` — the operator's
    own working trees are off limits;
  * an existing clone whose `origin` points somewhere other than the expected
    repo is refused rather than repointed.

Auth is delegated to `gh`, exactly like `infra/gh_cli.py`: every git invocation
carries `-c credential.helper=` (clear inherited helpers) followed by
`-c credential.https://github.com.helper=!gh auth git-credential`. That is the
same wiring `gh auth setup-git` writes globally, applied per-command so the
daemon never mutates the operator's git config. `GIT_TERMINAL_PROMPT=0` turns a
missing credential into a fast non-zero exit instead of a hung subprocess.

Error mapping:
    bad config / path guard tripped   → ConfigError    (boot-shaped, exit 78)
    unreachable commit, rejected push → PermanentError / TransientError per case
    network / transient git failure   → TransientError (dispatcher retries)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import signal
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from daeyeon_bot.core.errors import ConfigError, PermanentError, TransientError
from daeyeon_bot.core.pr_autofix.types import VerifyOutcome, WorkspaceDiff

_log = structlog.get_logger(__name__)

# Each segment must START with an alphanumeric or `_`. The obvious
# `[A-Za-z0-9._-]+/[A-Za-z0-9._-]+` accepts `../evil`, which would turn into the
# push URL `https://github.com/../evil.git` — a traversal handed straight to git.
_REPO_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*/[A-Za-z0-9_][A-Za-z0-9._-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# `git push` refuses a ref with these; catching them here gives a clear error
# instead of a cryptic git one, and stops a crafted ref from becoming an option.
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

_DEFAULT_GIT_TIMEOUT_S = 300.0
# Grace between SIGTERM and SIGKILL for a timed-out verify command.
_VERIFY_TERM_GRACE_S = 5.0
# Backstop wait after SIGKILL so a zombie cannot wedge the handler.
_VERIFY_REAP_TIMEOUT_S = 10.0
# Tail of the verify command's combined output kept for the audit row + reply.
_VERIFY_TAIL_CHARS = 4000

# Non-fast-forward push. Someone (a human, or another bot) pushed to the branch
# between our fetch and our push; the next poll re-reads the new head, so this
# is transient by construction — never force.
_PUSH_REJECTED_PHRASES = (
    "non-fast-forward",
    "failed to push some refs",
    "fetch first",
    "updates were rejected",
)


class PushRejectedError(TransientError):
    """The remote moved under us. Retry against the new head, never force."""


def _credential_args() -> tuple[str, ...]:
    """`-c` flags that route git's HTTPS auth through the operator's `gh`."""
    return (
        "-c",
        "credential.helper=",
        "-c",
        "credential.https://github.com.helper=!gh auth git-credential",
    )


def workspace_dir_name(repo: str) -> str:
    """`owner/repo` → `owner__repo`. Flat, collision-free, no nesting."""
    return repo.replace("/", "__")


@dataclass(slots=True)
class GitWorkspace:
    """One throwaway clone of one repo. Built per event, cheap to re-enter.

    `ensure_clone()` is idempotent: an existing valid clone is fetched, not
    re-cloned, so successive autofix rounds on the same repo stay fast.
    """

    repo: str
    workspace_root: Path
    project_root: Path | None = None
    allow_external: bool = False
    git_timeout_s: float = _DEFAULT_GIT_TIMEOUT_S
    author_name: str = "daeyeon-bot"
    author_email: str = "daeyeon-bot@users.noreply.github.com"
    # Override for the clone/push origin. Empty = derive `https://github.com/<repo>.git`.
    # Set it for a GitHub Enterprise host, an internal mirror, or (in tests) a
    # local bare repo standing in for GitHub.
    remote_url_override: str = ""
    path: Path = field(init=False)

    def __post_init__(self) -> None:
        if not _REPO_RE.match(self.repo):
            raise ConfigError(f"git_workspace: malformed repo {self.repo!r}")
        root = self.workspace_root.expanduser().resolve()
        home = Path.home().resolve()
        if home in (root, root.parent):
            raise ConfigError(
                f"git_workspace: workspace_root {root} sits directly in $HOME;"
                " the bot must never operate on the operator's own working trees"
            )
        if not self.allow_external and self.project_root is not None:
            project = self.project_root.expanduser().resolve()
            if not root.is_relative_to(project):
                raise ConfigError(
                    f"git_workspace: workspace_root {root} is outside project root"
                    f" {project}; set allow_external = true to override"
                )
        candidate = (root / workspace_dir_name(self.repo)).resolve()
        if not candidate.is_relative_to(root):
            raise ConfigError(f"git_workspace: clone path {candidate} escapes root {root}")
        self.workspace_root = root
        self.path = candidate

    # ── clone / checkout ──────────────────────────────────────────────────

    @property
    def remote_url(self) -> str:
        return self.remote_url_override or f"https://github.com/{self.repo}.git"

    async def ensure_clone(self) -> None:
        """Clone if absent, otherwise verify the remote and fetch.

        `--filter=blob:none` keeps the clone lazy: history metadata now, file
        blobs only when a checkout actually needs them. On a monorepo that is
        the difference between seconds and minutes per event.
        """
        git_dir = self.path / ".git"
        if await asyncio.to_thread(git_dir.exists):
            await self._assert_remote_matches()
        else:
            await asyncio.to_thread(self.workspace_root.mkdir, parents=True, exist_ok=True)
            await self._git_run(
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                self.remote_url,
                str(self.path),
                cwd=self.workspace_root,
                what="clone",
            )
        await self._git_run("fetch", "--prune", "origin", what="fetch")

    async def _assert_remote_matches(self) -> None:
        result = await self._git_run(
            "remote", "get-url", "origin", what="remote-get-url", check=False
        )
        actual = result[1].strip()
        if not self._remote_matches(actual):
            raise ConfigError(
                f"git_workspace: existing clone at {self.path} points at {actual!r},"
                f" not {self.repo!r}; refusing to repoint. Delete the directory to rebuild."
            )

    def _remote_matches(self, actual: str) -> bool:
        """True when the existing clone's `origin` addresses the repo we want.

        With an explicit `remote_url_override` the comparison is literal (we
        cannot infer `owner/repo` from an arbitrary host); otherwise any of
        git's github.com URL spellings is accepted.
        """
        if self.remote_url_override:
            return actual.strip().removesuffix(".git") == (
                self.remote_url_override.removesuffix(".git")
            )
        return _same_remote(actual, self.repo)

    async def checkout_pr(self, *, pr_number: int, head_sha: str) -> None:
        """Fetch `refs/pull/<n>/head` and hard-checkout `head_sha`.

        Going through the pull ref (rather than the head branch) is what makes
        fork PRs work — the fork's branch does not exist on `origin`, but every
        PR's head is always mirrored into the base repo's `refs/pull/*`.

        The tree is reset and cleaned first: a previous round's leftovers must
        never leak into this round's diff.
        """
        if not _SHA_RE.match(head_sha):
            raise PermanentError(f"git_workspace: malformed head_sha {head_sha!r}")
        await self._git_run(
            "fetch",
            "--force",
            "origin",
            f"+refs/pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}",
            what="fetch-pull-ref",
        )
        await self._git_run("checkout", "--force", "--detach", head_sha, what="checkout")
        await self._git_run("reset", "--hard", head_sha, what="reset")
        # `-x` is deliberately omitted: ignored build caches (.venv, __pycache__,
        # node_modules) are expensive to rebuild every round and never enter a
        # commit anyway, since `git add -A` honors .gitignore.
        await self._git_run("clean", "-fd", what="clean")

    # ── inspection ────────────────────────────────────────────────────────

    async def diff(self) -> WorkspaceDiff:
        """Summarize the working tree against HEAD, including untracked files.

        `--numstat` over `git add --intent-to-add -N` would mutate the index, so
        instead we take tracked changes from `diff HEAD` and untracked paths
        from `ls-files --others`, then merge. Binary files report `-` for their
        counts in numstat; those contribute files but not lines.
        """
        _, tracked, _ = await self._git_run(
            "diff", "--numstat", "HEAD", what="diff-numstat", check=False
        )
        _, untracked, _ = await self._git_run(
            "ls-files", "--others", "--exclude-standard", what="ls-files-others", check=False
        )
        files: list[str] = []
        insertions = 0
        deletions = 0
        for line in tracked.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_raw, del_raw, path = parts[0], parts[1], parts[-1]
            files.append(path)
            if add_raw.isdigit():
                insertions += int(add_raw)
            if del_raw.isdigit():
                deletions += int(del_raw)
        for line in untracked.splitlines():
            path = line.strip()
            if not path:
                continue
            files.append(path)
            insertions += await self._count_lines(path)
        return WorkspaceDiff(
            changed_files=tuple(dict.fromkeys(files)),
            insertions=insertions,
            deletions=deletions,
        )

    async def _count_lines(self, relative_path: str) -> int:
        """Line count of a new untracked file; 0 for binary or unreadable."""
        target = (self.path / relative_path).resolve()
        if not target.is_relative_to(self.path):
            return 0
        try:
            text = await asyncio.to_thread(target.read_text, encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return 0
        return len(text.splitlines())

    async def revert_working_tree(self) -> None:
        """Throw away uncommitted edits, leaving HEAD where it is.

        Called when a gate rejects the agent's work (protected path, oversized
        diff). Without it the next round's `checkout_pr` would still clean the
        tree, but leaving a rejected diff sitting on disk between events makes
        the workspace confusing to inspect after an incident.
        """
        await self._git_run("reset", "--hard", "HEAD", what="revert-reset", check=False)
        await self._git_run("clean", "-fd", what="revert-clean", check=False)

    # ── verify / commit / push ────────────────────────────────────────────

    async def run_verify(self, command: str, *, timeout_s: float) -> VerifyOutcome:
        """Run the repo's configured pre-push check inside the workspace.

        Executed via the user's shell so operators can write `just check` or
        `make lint && pytest -q` without this module growing a mini-parser.
        A timeout is reported as a non-zero exit, not an exception — a slow
        verify is a *failed* verify, and the handler's "don't push" branch is
        already the right response.

        `start_new_session=True` is load-bearing, not hygiene. A shell command
        is `/bin/sh -c "..."` with the real work as a CHILD of that shell, so
        `proc.kill()` reaps only the shell and leaves the build running — still
        holding the stdout pipe, so the await does not even return until the
        orphan finishes on its own. That turns `verify_timeout_seconds` into a
        suggestion. Putting the command in its own process group lets the
        timeout path signal the whole tree.
        """
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=self._env(),
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            await self._kill_process_group(proc)
            return VerifyOutcome(
                command=command,
                exit_code=124,
                output_tail=f"verify command timed out after {timeout_s:.0f}s",
            )
        text = stdout.decode("utf-8", errors="replace")
        return VerifyOutcome(
            command=command,
            exit_code=proc.returncode if proc.returncode is not None else 1,
            output_tail=text[-_VERIFY_TAIL_CHARS:],
        )

    async def _kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the timed-out command's whole process group, then SIGKILL.

        The grace period lets a build tool remove its temp dirs; the follow-up
        SIGKILL is what guarantees the handler slot is actually released. The
        group id equals `proc.pid` because the process was started with
        `start_new_session=True`.
        """
        for sig, grace in ((signal.SIGTERM, _VERIFY_TERM_GRACE_S), (signal.SIGKILL, None)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return  # already gone
            except PermissionError:  # pragma: no cover — should not happen for our own child
                _log.warning("git_workspace.killpg_denied", pid=proc.pid, signal=sig.name)
                break
            if grace is None:
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except TimeoutError:
                continue
            else:
                return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_VERIFY_REAP_TIMEOUT_S)

    async def commit_all(self, message: str) -> str:
        """Stage every change and commit. Returns the new commit SHA.

        Identity is passed with `-c` rather than written into the clone's
        config, so the workspace stays a pure derived artifact.
        """
        await self._git_run("add", "-A", what="add")
        await self._git_run(
            "-c",
            f"user.name={self.author_name}",
            "-c",
            f"user.email={self.author_email}",
            "commit",
            "--no-verify",
            "-m",
            message,
            what="commit",
        )
        _, sha, _ = await self._git_run("rev-parse", "HEAD", what="rev-parse")
        return sha.strip()

    async def push(self, *, head_repo: str, head_ref: str) -> None:
        """Push HEAD to the PR's head branch.

        `head_repo` is `pr.head.repo.full_name` — for a fork PR that is the
        fork, not `self.repo`, so the push target is computed from the PR
        payload rather than assumed. No `--force`, ever: a rejected push means
        the branch moved and the right answer is to re-read it next round.
        """
        if not _REPO_RE.match(head_repo):
            raise PermanentError(f"git_workspace: malformed head_repo {head_repo!r}")
        if not head_ref or not _REF_RE.match(head_ref) or head_ref.startswith("-"):
            raise PermanentError(f"git_workspace: malformed head_ref {head_ref!r}")
        # A fork PR pushes to the FORK, so the target is derived from the PR
        # payload, not from `self.repo`. An override host is assumed to serve
        # every repo under the same root (true for GHE and for a test bare repo).
        target = (
            self.remote_url_override
            if self.remote_url_override and head_repo == self.repo
            else f"https://github.com/{head_repo}.git"
        )
        code, _, stderr = await self._git_run(
            "push", target, f"HEAD:refs/heads/{head_ref}", what="push", check=False
        )
        if code == 0:
            return
        lowered = stderr.lower()
        if any(phrase in lowered for phrase in _PUSH_REJECTED_PHRASES):
            raise PushRejectedError(
                f"git push rejected for {head_repo}:{head_ref} (branch moved): {stderr.strip()}"
            )
        raise TransientError(f"git push failed for {head_repo}:{head_ref}: {stderr.strip()}")

    # ── internals ─────────────────────────────────────────────────────────

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # A missing credential must fail fast; a daemon has no terminal to
        # answer a username prompt on, and a hung git holds the handler slot.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        return env

    async def _git_run(
        self,
        *args: str,
        cwd: Path | None = None,
        what: str,
        check: bool = True,
    ) -> tuple[int, str, str]:
        """Run one git command. Returns `(returncode, stdout, stderr)`."""
        full = ("git", *_credential_args(), *args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *full,
                cwd=str(cwd if cwd is not None else self.path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=self._env(),
            )
        except FileNotFoundError as exc:
            # `create_subprocess_exec` raises this for BOTH "git is not on PATH"
            # and "cwd does not exist". Reporting the wrong one sends the
            # operator hunting for a missing binary that is right there.
            target = cwd if cwd is not None else self.path
            if not await asyncio.to_thread(target.is_dir):
                raise ConfigError(
                    f"git {what}: workspace {target} does not exist (was `ensure_clone()` skipped?)"
                ) from exc
            raise ConfigError(f"git not found on PATH: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.git_timeout_s)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise TransientError(f"git {what} timed out after {self.git_timeout_s:.0f}s") from exc
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        code = proc.returncode if proc.returncode is not None else 1
        if code != 0:
            _log.warning(
                "git_workspace.command_failed",
                what=what,
                repo=self.repo,
                returncode=code,
                stderr=err.strip()[:500],
            )
            if check:
                raise TransientError(
                    f"git {what} failed (exit {code}): {err.strip() or out.strip()}"
                )
        return (code, out, err)


def _same_remote(actual: str, repo: str) -> bool:
    """True if `actual` (any git URL form) addresses `repo`."""
    normalized = actual.strip().removesuffix(".git")
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].lower() == repo.lower()
    return False


def format_verify_command(command: str) -> str:
    """Render a verify command for a GitHub comment without breaking markdown."""
    return shlex.join(shlex.split(command)) if command else ""


__all__ = [
    "GitWorkspace",
    "PushRejectedError",
    "format_verify_command",
    "workspace_dir_name",
]
