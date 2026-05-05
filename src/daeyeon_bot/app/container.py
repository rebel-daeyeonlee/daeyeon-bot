"""Composition root.

The ONLY place where concrete `infra` adapters and `triggers` / `handlers`
plugins are wired together. Tests build their own container with fakes.

Production wiring is intentionally tiny — just enough to hold the components
the dispatcher and CLI need. Real DI complexity (heartbeat / supervisor /
secrets) lands as later phases come online.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

from daeyeon_bot.app import pause as pause_mod
from daeyeon_bot.app.config import Config
from daeyeon_bot.app.registry import (
    GhReviewRequestedDeps,
    HandlerRegistry,
    PrReviewDeps,
    TriggerRecord,
    build_handler_registry,
    build_trigger_registry,
)
from daeyeon_bot.app.supervisor import TriggerSupervisor
from daeyeon_bot.core.errors import QuotaError
from daeyeon_bot.core.time import Clock, SystemClock
from daeyeon_bot.handlers.pr_review import PauseGuard
from daeyeon_bot.infra import storage
from daeyeon_bot.infra.claude import ClaudeSession, make_real_factory
from daeyeon_bot.infra.gh_cli import GhCli
from daeyeon_bot.infra.pr_review_persona import PersonaLoader

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Container:
    """Aggregate of wired-up components for one daemon process."""

    config: Config
    clock: Clock
    db: aiosqlite.Connection
    handlers: HandlerRegistry
    triggers: tuple[TriggerRecord, ...]
    claude_session_factory: Callable[[], ClaudeSession]
    gh: object | None
    persona_loader: PersonaLoader | None
    github_username: str | None


@dataclass(frozen=True, slots=True)
class ContainerOverrides:
    """Optional swap-ins for tests (or for `--insecure-env` style debug runs)."""

    clock: Clock | None = None
    claude_session_factory: Callable[[], ClaudeSession] | None = None
    gh: object | None = None  # GhCli or a FakeGh
    persona_loader: PersonaLoader | None = None
    github_username: str | None = None
    pause_guard: PauseGuard | None = None


async def build(
    config: Config,
    db: aiosqlite.Connection,
    *,
    oauth_token: str | None = None,
    overrides: ContainerOverrides | None = None,
) -> Container:
    """Wire concrete dependencies. `db` must already be opened + migrated.

    Async because resolving `github.username` may require one boot-time
    `gh api /user` round-trip when the operator has not pinned it in config.
    """
    overrides = overrides or ContainerOverrides()
    factory = overrides.claude_session_factory or _build_real_factory(config, oauth_token)

    pr_review_enabled = _pr_review_enabled(config)
    gh: object | None = None
    persona_loader: PersonaLoader | None = None
    github_username: str | None = None
    pr_deps: PrReviewDeps | None = None
    clock = overrides.clock or SystemClock()

    if pr_review_enabled:
        gh = overrides.gh or GhCli(timeout_seconds=config.github.gh_call_timeout_seconds)
        persona_loader = overrides.persona_loader or PersonaLoader(
            skills_root=_resolve_skills_root(config),
        )
        github_username = await _resolve_github_username(
            override=overrides.github_username,
            configured=config.github.username,
            gh=gh,
        )
        pause_guard = overrides.pause_guard or _make_pause_guard(config)
        pr_deps = PrReviewDeps(
            gh=gh,
            persona_loader=persona_loader,
            db=db,
            github_username=github_username,
            pause_guard=pause_guard,
        )

    gh_trigger_deps = _build_gh_review_requested_deps(
        config=config,
        gh=gh,
        github_username=github_username,
        clock=clock,
    )

    triggers = build_trigger_registry(config, gh_review_requested_deps=gh_trigger_deps)

    return Container(
        config=config,
        clock=clock,
        db=db,
        handlers=build_handler_registry(config, pr_review_deps=pr_deps),
        triggers=tuple(triggers),
        claude_session_factory=factory,
        gh=gh,
        persona_loader=persona_loader,
        github_username=github_username,
    )


def _pr_review_enabled(config: Config) -> bool:
    entry = config.handlers.get("pr_review")
    return entry is not None and entry.enabled


def _resolve_skills_root(config: Config) -> Path | None:
    """Override path for `<skills_root>/<persona_skill>/SKILL.md`, or None for default."""
    raw = config.pr_review_handler_entry().skills_root
    if not raw:
        return None
    return Path(raw).expanduser()


async def _resolve_github_username(*, override: str | None, configured: str, gh: object) -> str:
    """`override` (test injection) → `[github] username` → `gh api /user` fallback."""
    if override:
        return override
    if configured:
        return configured
    if not isinstance(gh, GhCli):
        raise RuntimeError(
            "container.build: github.username unset and gh override is not a real GhCli"
        )
    login = await gh.auth_user()
    _log.info("container.github_username_resolved_from_gh", username=login)
    return login


def _make_pause_guard(config: Config) -> PauseGuard:
    """Async wrapper that raises QuotaError when the PAUSE flag is present."""
    flag_path = config.pause_flag_path

    async def _guard() -> None:
        if pause_mod.is_paused(flag_path):
            raise QuotaError("paused")

    return _guard


def _build_real_factory(config: Config, oauth_token: str | None) -> Callable[[], ClaudeSession]:
    if oauth_token is None:
        raise RuntimeError(
            "container.build: oauth_token required when no claude_session_factory override"
        )
    return make_real_factory(
        oauth_token=oauth_token,
        model=config.claude.model,
        default_system_prompt=config.claude.default_system_prompt,
    )


def _build_gh_review_requested_deps(
    *,
    config: Config,
    gh: object | None,
    github_username: str | None,
    clock: Clock,
) -> GhReviewRequestedDeps | None:
    """Assemble `GhReviewRequestedDeps` when the trigger is enabled in config.

    The polling trigger writes events directly through SQLite, so it owns
    its own short-lived connections — `storage_factory` is the bridge.
    """
    entry = config.triggers.get("gh_review_requested")
    if entry is None or not entry.enabled:
        return None
    if gh is None or github_username is None:
        # The trigger needs the same GitHub deps the handler does. If the
        # operator has the trigger enabled but `[handlers.pr_review]` off,
        # the trigger has nothing to drive — skip silently.
        return None
    db_path = config.db_path

    def _storage_factory() -> Any:
        return storage.connection(db_path)

    pause_flag_path = config.pause_flag_path

    def _pause_check() -> bool:
        return pause_mod.is_paused(pause_flag_path)

    supervisor = TriggerSupervisor()

    async def _report_permanent_failure(reason: str) -> bool:
        async with storage.connection(db_path) as conn:
            return await supervisor.record_failure(
                conn,
                trigger_name="gh_review_requested",
                reason=reason,
                at=clock.now(),
            )

    return GhReviewRequestedDeps(
        gh=gh,
        storage_factory=_storage_factory,
        github_username=github_username,
        clock=clock,
        pause_check=_pause_check,
        permanent_failure_reporter=_report_permanent_failure,
    )
