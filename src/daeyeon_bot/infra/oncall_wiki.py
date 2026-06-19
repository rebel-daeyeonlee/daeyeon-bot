"""Manager for the bot's read-only clone of the OnCall LLM Wiki.

Lives at `<project_root>/var/ssw-devops-oncall/` by default. Read-only:
`ensure_fresh()` clones-if-absent then `git pull --ff-only`; there is NO commit/
push/reset method. `ls_remote()` is a boot reachability probe. `search()` does
signature-first ripgrep over the incident canonicals + the recovery playbook.

Two-layer path guard (verbatim from `ssw_bundle.py`): hard-ban the operator's
working vault `~/ssw-devops-oncall` regardless of `allow_external`, then require
the clone to live inside `project_root`.

Headless-safe git env (stronger than `ssw_bundle.py`, which assumes an
already-cloned tree on a dev machine): this adapter does the FIRST clone of a
brand-new remote under a TTY-less, agent-less launchd/systemd process, so every
git invocation sets `GIT_TERMINAL_PROMPT=0` + a `GIT_SSH_COMMAND` with
`BatchMode=yes` (never prompt), `StrictHostKeyChecking=accept-new` (trust-on-
first-contact then pin — achievable here because this is git-over-OpenSSH, not
asyncssh as in `ssh_logs.py`), and a managed 0600 `UserKnownHostsFile`. The 0600
perm-check reuses the `ssh_logs.py:74-86` discipline. See
specs/003-ci-monitor-bot/plan.md §infra/oncall_wiki.py.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat as stat_mod
from dataclasses import dataclass, field
from pathlib import Path

from daeyeon_bot.core.ci_triage.types import WikiMatch
from daeyeon_bot.core.errors import AuthError, ConfigError

# Pinned vault-relative paths (single source of truth; mirror in
# contracts/oncall-wiki-surface.md). Used by (a) the ripgrep search scope and
# (b) the corrupt-clone sentinel check, so they must match the live vault.
INCIDENTS_DIR = "wiki/oncall/incidents"
RECOVERY_PLAYBOOK_PATH = "wiki/notes/recovery-playbook.md"

_AUTH_PHRASES = (
    "permission denied",
    "authentication failed",
    "could not read from remote repository",
    "access denied",
    "fatal: could not read username",
)


@dataclass(frozen=True, slots=True)
class WikiRefresh:
    """Outcome of `ensure_fresh()`. The handler degrades on `not available`
    rather than retrying (it has no attempt counter)."""

    available: bool  # is there a usable clone to search?
    stale: bool  # True when pull failed but a prior healthy clone remains
    error: str | None = None  # short label for the audit `wiki_error`


@dataclass(slots=True)
class OncallWiki:
    """One per daemon. Path-guarded, read-only git clone + ripgrep search."""

    clone_path: Path
    known_hosts_path: Path
    remote_url: str = "git@github.com:rebellions-sw/ssw-devops-oncall.git"
    project_root: Path | None = None
    allow_external: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        path_resolved = self.clone_path.expanduser().resolve()
        # Layer 1 — hard-ban the operator's working vault, regardless of allow_external.
        forbidden = (Path.home() / "ssw-devops-oncall").resolve()
        if path_resolved == forbidden:
            raise ConfigError(
                f"oncall_wiki: refusing to operate on operator working vault {forbidden}"
            )
        # Layer 2 — must live inside project_root unless allow_external.
        if self.project_root is not None:
            root_resolved = self.project_root.expanduser().resolve()
            try:
                path_resolved.relative_to(root_resolved)
                inside_root = True
            except ValueError:
                inside_root = False
            if not inside_root and not self.allow_external:
                raise ConfigError(
                    f"oncall_wiki: clone_path {path_resolved} is outside project root"
                    f" {root_resolved}; set allow_external=true to override"
                )
        self.clone_path = path_resolved
        # Establish the managed 0600 known_hosts file BEFORE any git/ls_remote op
        # (accept-new creates entries on first contact but the file/dir must exist
        # and be 0600). Mirror ssh_logs.py:74-86.
        self._ensure_known_hosts()

    def _ensure_known_hosts(self) -> None:
        if self.known_hosts_path.exists():
            mode = self.known_hosts_path.stat().st_mode & 0o777
            if mode & (stat_mod.S_IRGRP | stat_mod.S_IROTH | stat_mod.S_IWGRP | stat_mod.S_IWOTH):
                raise PermissionError(
                    f"oncall_wiki: known_hosts file {self.known_hosts_path} has perms"
                    f" {oct(mode)}; expected 0o600"
                )
        else:
            self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
            self.known_hosts_path.touch(mode=0o600, exist_ok=False)

    def _git_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_SSH_COMMAND"] = (
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
            f" -o UserKnownHostsFile={self.known_hosts_path}"
        )
        return env

    # ── boot probe ────────────────────────────────────────────────────────────

    async def ls_remote(self) -> None:
        """Boot reachability probe: `git ls-remote --quiet <remote_url>` under the
        headless-safe env. Fails LOUD at boot (ConfigError / AuthError → exit 78)
        so a missing deploy key / unreachable remote never becomes a 600 s
        first-triage hang."""
        rc, _out, err = await self._git_run(
            ["ls-remote", "--quiet", self.remote_url],
            cwd=self.clone_path.parent if self.clone_path.parent.exists() else Path.cwd(),
        )
        if rc != 0:
            lower = err.lower()
            if any(p in lower for p in _AUTH_PHRASES):
                raise AuthError(f"oncall_wiki: ls-remote auth failure: {err.strip()}")
            raise ConfigError(f"oncall_wiki: remote {self.remote_url} unreachable: {err.strip()}")

    # ── refresh (clone / pull) ──────────────────────────────────────────────────

    def _is_healthy_clone(self) -> bool:
        """A clone is healthy only if `.git` AND the sentinel tracked file (the
        recovery playbook — always needed by search) are present & non-empty.
        Guards against a partial/corrupt clone (`.git` present, empty worktree)."""
        if not (self.clone_path / ".git").exists():
            return False
        sentinel = self.clone_path / RECOVERY_PLAYBOOK_PATH
        try:
            return sentinel.is_file() and sentinel.stat().st_size > 0
        except OSError:
            return False

    async def ensure_fresh(self) -> WikiRefresh:
        """Clone if absent/corrupt, else `git pull --ff-only`. Never raises for a
        transient git failure — returns a `WikiRefresh` the handler degrades on."""
        async with self._lock:
            if self._is_healthy_clone():
                rc, _out, err = await self._git_run(["pull", "--ff-only"], cwd=self.clone_path)
                if rc != 0:
                    return WikiRefresh(
                        available=True, stale=True, error=f"pull_failed:{err.strip()[:120]}"
                    )
                return WikiRefresh(available=True, stale=False)

            # No healthy clone: a partial/corrupt dir is removed and re-cloned
            # (var/ is gitignored and fully rebuildable).
            if self.clone_path.exists():
                shutil.rmtree(self.clone_path, ignore_errors=True)
            self.clone_path.parent.mkdir(parents=True, exist_ok=True)
            rc, _out, err = await self._git_run(
                ["clone", "--depth", "1", self.remote_url, str(self.clone_path)],
                cwd=self.clone_path.parent,
            )
            if rc != 0 or not self._is_healthy_clone():
                return WikiRefresh(
                    available=False, stale=False, error=f"clone_failed:{err.strip()[:120]}"
                )
            return WikiRefresh(available=True, stale=False)

    # ── search ──────────────────────────────────────────────────────────────────

    async def search(
        self,
        *,
        signatures: tuple[str, ...],
        phrases: tuple[str, ...],
    ) -> list[WikiMatch]:
        """Signature-first keyword search over `incidents/`, always including the
        recovery playbook. A multi-word phrase matched against an incident
        `signature:` frontmatter line scores highest; a body match scores lower;
        a lone token lowest. Returns matches sorted by descending score.

        Implemented as a pure-Python scan (like `ssw_bundle.grep_test_case`) — the
        vault is a small Obsidian repo, so a `re` scan is fast and avoids depending
        on `rg` being a real PATH binary in a headless deploy. Async signature kept
        for interface stability; the I/O is synchronous file reads."""
        return self._scan(signatures=signatures, phrases=phrases)

    def _scan(
        self,
        *,
        signatures: tuple[str, ...],
        phrases: tuple[str, ...],
    ) -> list[WikiMatch]:
        incidents_dir = self.clone_path / INCIDENTS_DIR
        scored: dict[str, WikiMatch] = {}
        terms = [t.strip() for t in (*signatures, *phrases) if t and t.strip()]

        files = sorted(incidents_dir.rglob("*.md")) if incidents_dir.exists() else []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = self._relpath(f)
            sig_match = re.search(r"(?im)^signature:\s*(.+)$", text)
            sig_line = sig_match.group(1) if sig_match else ""
            lowered = text.lower()
            for term in terms:
                tl = term.lower()
                weight = 3 if len(term.split()) >= 2 else 1
                if sig_line and tl in sig_line.lower():
                    self._add(
                        scored,
                        rel,
                        signature_matched=True,
                        score=10 * weight,
                        snippet=sig_line.strip(),
                    )
                elif tl in lowered:
                    self._add(
                        scored,
                        rel,
                        signature_matched=False,
                        score=2 * weight,
                        snippet=_first_line_with(text, tl),
                    )

        # Always include the recovery playbook (the on-call first-response index).
        playbook = self.clone_path / RECOVERY_PLAYBOOK_PATH
        if playbook.is_file() and RECOVERY_PLAYBOOK_PATH not in scored:
            scored[RECOVERY_PLAYBOOK_PATH] = WikiMatch(
                path=RECOVERY_PLAYBOOK_PATH,
                signature_matched=False,
                score=1,
                snippet="(recovery playbook — always included)",
            )

        return sorted(scored.values(), key=lambda m: m.score, reverse=True)

    def _add(
        self,
        scored: dict[str, WikiMatch],
        rel: str,
        *,
        signature_matched: bool,
        score: int,
        snippet: str,
    ) -> None:
        prev = scored.get(rel)
        if prev is None:
            scored[rel] = WikiMatch(
                path=rel, signature_matched=signature_matched, score=score, snippet=snippet
            )
        elif score > prev.score:
            scored[rel] = WikiMatch(
                path=rel,
                signature_matched=signature_matched or prev.signature_matched,
                score=score,
                snippet=prev.snippet,
            )

    def _relpath(self, abs_path: Path) -> str:
        try:
            return str(abs_path.resolve().relative_to(self.clone_path))
        except ValueError:
            return str(abs_path)

    async def _git_run(self, args: list[str], *, cwd: Path) -> tuple[int, str, str]:
        """Run `git <args>` under the headless-safe env. Returns (rc, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            env=self._git_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        return (
            proc.returncode or 0,
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"),
        )


def _first_line_with(text: str, needle_lower: str) -> str:
    """First line of `text` containing `needle_lower` (case-insensitive), trimmed."""
    for line in text.splitlines():
        if needle_lower in line.lower():
            return line.strip()[:200]
    return ""


__all__ = [
    "INCIDENTS_DIR",
    "RECOVERY_PLAYBOOK_PATH",
    "OncallWiki",
    "WikiRefresh",
]
