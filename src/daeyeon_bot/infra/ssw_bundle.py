"""Manager for the bot's dedicated ssw-bundle clone.

Lives at `<project_root>/var/ssw-bundle/` by default. The bot reads
files at a given commit via:
  1. `git fetch --prune --filter=blob:none origin`
  2. `git checkout --force --detach <commit_sha>`
  3. `git submodule update --init --depth 1`  (top-level only, not
     recursive — see ensure_checkout for why)

Path guards (constructor-time):
  - Refuses if clone_path resolves outside project_root unless
    `allow_external=True`.
  - ALWAYS refuses if clone_path resolves to `~/ssw-bundle` (the
    operator's working tree).
  - Refuses if an existing `.git/config` points at a remote URL other
    than the configured one.

Only read-side helpers are exposed (`read_file`, `grep_test_case`).
There is no `push`, `commit`, or arbitrary-command escape hatch.

See `specs/002-jira-triage-bot/contracts/ssw-bundle-checkout-surface.md`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from daeyeon_bot.core.errors import ConfigError, PermanentError, TransientError

# 7-40 hex — short SHAs are accepted by `git checkout`; SSWCI Epic
# descriptions sometimes carry only 7-char shorts (e.g. `2486620`). If
# the short form is ambiguous in the clone, the subsequent
# `git checkout` exits non-zero and we surface
# `UnresolvableCommitError` → audit `skipped_unresolvable_commit`.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class UnresolvableCommitError(PermanentError):
    """git fetch + checkout couldn't reach the requested commit."""


class SubmoduleInitError(PermanentError):
    """git submodule update --init failed."""

    def __init__(self, message: str, failed_paths: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.failed_paths = failed_paths


@dataclass(slots=True)
class SswBundleClient:
    """One per daemon. Path-guarded git ops + read-only file access."""

    clone_path: Path
    remote_url: str = "git@github.com:rebellions-sw/ssw-bundle.git"
    project_root: Path | None = None
    allow_external: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        # Path guards.
        path_resolved = self.clone_path.expanduser().resolve()
        # Hard-ban: operator's working tree.
        home = Path.home()
        forbidden = (home / "ssw-bundle").resolve()
        if path_resolved == forbidden:
            raise ConfigError(
                f"ssw_bundle: refusing to operate on operator working tree {forbidden}"
            )
        if self.project_root is not None:
            root_resolved = self.project_root.expanduser().resolve()
            try:
                path_resolved.relative_to(root_resolved)
                inside_root = True
            except ValueError:
                inside_root = False
            if not inside_root and not self.allow_external:
                raise ConfigError(
                    f"ssw_bundle: clone_path {path_resolved} is outside project root"
                    f" {root_resolved}; set allow_external_ssw_bundle=true to override"
                )
        # If the clone already exists, verify its origin URL matches.
        git_config = path_resolved / ".git" / "config"
        if git_config.exists():
            cfg_text = git_config.read_text(encoding="utf-8", errors="ignore")
            origin_block = _parse_origin_url(cfg_text)
            if origin_block and origin_block != self.remote_url:
                raise ConfigError(
                    f"ssw_bundle: existing clone at {path_resolved} has remote.origin.url"
                    f" {origin_block!r}; expected {self.remote_url!r}"
                )
        # Persist the resolved path so subsequent ops use the canonical form.
        self.clone_path = path_resolved

    # ── git ops ─────────────────────────────────────────────────────────────

    async def ensure_clone(self) -> None:
        """Clone the remote into `clone_path` if `.git` is absent. Idempotent."""
        if (self.clone_path / ".git").exists():
            return
        self.clone_path.parent.mkdir(parents=True, exist_ok=True)
        await self._git_run(
            [
                "clone",
                "--filter=blob:none",
                self.remote_url,
                str(self.clone_path),
            ],
            cwd=self.clone_path.parent,
            op="clone",
        )

    async def ensure_checkout(self, *, branch: str, commit_sha: str) -> None:
        """Fetch origin, checkout commit (detached), init submodules.

        `branch` is informational (used in logs); `commit_sha` is the
        source of truth. Raises `UnresolvableCommitError` if the SHA is
        not reachable, or `SubmoduleInitError` if a submodule init fails.
        """
        if not _COMMIT_SHA_RE.match(commit_sha):
            raise PermanentError(f"ssw_bundle: invalid commit sha {commit_sha!r}")

        async with self._lock:
            await self.ensure_clone()
            # 1. Fetch.
            await self._git_run(
                ["fetch", "--prune", "--filter=blob:none", "origin"],
                cwd=self.clone_path,
                op="fetch",
            )
            # 2. Checkout (detached). If the commit isn't reachable, git exits non-zero.
            try:
                await self._git_run(
                    ["checkout", "--force", "--detach", commit_sha],
                    cwd=self.clone_path,
                    op="checkout",
                )
            except TransientError as exc:
                raise UnresolvableCommitError(
                    f"ssw_bundle: commit {commit_sha} unresolvable on branch {branch}: {exc}"
                ) from exc
            # 3. Submodules — top-level only, NOT recursive.
            #
            # Triage reads source under `products/<vendor>/<comp>/` which
            # lives in first-level submodules (products/common/umd,
            # products/atom/fw, ...). Recursive init follows their internal
            # vendor chains (umd → rbln-spdm → SPDM-Responder-Validator →
            # libspdm → openssl → krb5) — hundreds of MB of crypto/protocol
            # deps the bot never reads, plus a slow chain that fails on
            # `index.lock` collisions when interrupted mid-fetch.
            stdout = ""
            stderr = ""
            try:
                stdout, stderr = await self._git_run(
                    ["submodule", "update", "--init", "--depth", "1"],
                    cwd=self.clone_path,
                    op="submodule_update",
                )
            except TransientError as exc:
                raise SubmoduleInitError(f"ssw_bundle: submodule init failed: {exc}") from exc
            # Defensive: parse stderr for "fatal: clone of '...' into ..." paths.
            failed_paths = _parse_failed_submodules(stdout + "\n" + stderr)
            if failed_paths:
                raise SubmoduleInitError(
                    f"ssw_bundle: submodules failed: {', '.join(failed_paths)}",
                    failed_paths=failed_paths,
                )

    # ── read-only access ────────────────────────────────────────────────────

    def read_file(self, relative_path: str) -> str | None:
        """Read a file at the current checkout. Refuses paths that escape clone."""
        candidate = (self.clone_path / relative_path).resolve()
        try:
            candidate.relative_to(self.clone_path)
        except ValueError as exc:
            raise PermanentError(
                f"ssw_bundle: read_file refused {relative_path!r} — outside clone"
            ) from exc
        if not candidate.is_file():
            return None
        return candidate.read_text(encoding="utf-8", errors="replace")

    def grep_test_case(self, *, tc_name: str) -> Path | None:
        """Search `test/system/suites/**/*.robot` for `Test Case` block named `tc_name`.

        Returns the path relative to the clone root, or None.
        Implementation: Python re scan — fast enough at the scale (one tree).
        """
        suites = self.clone_path / "test" / "system" / "suites"
        if not suites.exists():
            return None
        # Robot test-case heading conventions: name at column 0, *** Test Cases *** section.
        for robot_file in suites.rglob("*.robot"):
            try:
                text = robot_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # The TC name appears at column 0, anywhere in the file, on its own
            # line under a `*** Test Cases ***` section. Simplest heuristic:
            # the literal name at column 0.
            pattern = re.compile(rf"(?m)^{re.escape(tc_name)}\s*$")
            if pattern.search(text):
                return robot_file.relative_to(self.clone_path)
        return None

    # ── plumbing ────────────────────────────────────────────────────────────

    async def _git_run(
        self,
        args: list[str],
        *,
        cwd: Path,
        op: str,
    ) -> tuple[str, str]:
        """Run `git <args>`. Returns (stdout, stderr) on rc=0; raises on non-zero."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise TransientError(
                f"git {op} (rc={proc.returncode}): {stderr.strip() or stdout.strip()}"
            )
        return (stdout, stderr)


# ── Helpers ──────────────────────────────────────────────────────────────────


_ORIGIN_BLOCK_RE = re.compile(
    r"\[remote\s+\"origin\"\][^\[]*?url\s*=\s*(?P<url>\S+)",
    re.DOTALL,
)


def _parse_origin_url(config_text: str) -> str | None:
    """Extract `remote.origin.url` from a git config text blob."""
    match = _ORIGIN_BLOCK_RE.search(config_text)
    if match is None:
        return None
    return match.group("url").strip()


_FAILED_SUBMODULE_RE = re.compile(
    r"(?:^|\n)\s*(?:fatal: )?(?:clone of '.*' into submodule path |Failed to (?:clone|recurse into) )"
    r"['\"]?(?P<path>[^'\"]+)",
)


def _parse_failed_submodules(text: str) -> tuple[str, ...]:
    """Return submodule paths that the update reported as failed."""
    found: list[str] = []
    for m in _FAILED_SUBMODULE_RE.finditer(text):
        path = m.group("path").strip().strip(":")
        if path and path not in found:
            found.append(path)
    return tuple(found)


__all__ = [
    "SswBundleClient",
    "SubmoduleInitError",
    "UnresolvableCommitError",
]
