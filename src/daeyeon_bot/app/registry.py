"""Plugin discovery and routing table construction.

Keeps trigger / handler instantiation out of `container.py` so the composition
root stays small and tests can build narrow registries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from daeyeon_bot.app.config import (
    Config,
    GhReviewRequestedTriggerEntry,
    HandlerEntry,
    PrReviewHandlerEntry,
)
from daeyeon_bot.core.errors import ConfigError
from daeyeon_bot.core.manifest import HandlerManifest, TriggerManifest
from daeyeon_bot.core.time import Clock
from daeyeon_bot.handlers import echo as echo_handler
from daeyeon_bot.handlers import pr_review as pr_review_handler
from daeyeon_bot.handlers.pr_review import PauseGuard
from daeyeon_bot.infra.pr_review_persona import PersonaLoader
from daeyeon_bot.triggers import gh_review_requested as gh_review_requested_trigger
from daeyeon_bot.triggers.gh_review_requested import StorageFactory


@dataclass(frozen=True, slots=True)
class HandlerRecord:
    """An instantiated handler ready to dispatch."""

    name: str
    manifest: HandlerManifest
    instance: object  # actual Handler — typing.Protocol does not enforce at runtime here.


@dataclass(frozen=True, slots=True)
class PrReviewDeps:
    """Runtime dependencies the `pr_review` handler can't fabricate itself.

    Inspection-only callers (`daeyeon-bot inspect handlers ls`) call
    `build_handler_registry` without these deps; the registry skips
    `pr_review` in that case so listing config doesn't require booting
    `gh` / persona / SQLite. The dispatcher path (via `container.build`)
    must always pass them in.
    """

    gh: Any
    persona_loader: PersonaLoader
    db: Any
    github_username: str
    pause_guard: PauseGuard | None = None


@dataclass(slots=True)
class HandlerRegistry:
    """Registry the dispatcher consults to look up a handler by name."""

    by_name: dict[str, HandlerRecord] = field(default_factory=dict[str, HandlerRecord])
    routing: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def register(self, record: HandlerRecord) -> None:
        if record.name in self.by_name:
            raise ConfigError(f"duplicate handler name: {record.name}")
        self.by_name[record.name] = record

    def handlers_for(self, event_type: str) -> list[HandlerRecord]:
        names = self.routing.get(event_type, [])
        return [self.by_name[n] for n in names if n in self.by_name]


def _override_manifest(manifest: HandlerManifest, entry: HandlerEntry) -> HandlerManifest:
    """Apply config overrides on top of the compile-time manifest."""
    kwargs = {}
    if entry.idempotent is not None:
        kwargs["idempotent"] = entry.idempotent
    if entry.dedup_ttl_seconds is not None:
        kwargs["dedup_ttl"] = timedelta(seconds=entry.dedup_ttl_seconds)
    if entry.side_effect_key is not None:
        kwargs["side_effect_key"] = entry.side_effect_key
    if entry.concurrency is not None:
        kwargs["concurrency"] = entry.concurrency
    if entry.accepts is not None:
        kwargs["accepts"] = tuple(entry.accepts)
    if not kwargs:
        return manifest
    return replace(manifest, **kwargs)


def build_handler_registry(
    config: Config,
    *,
    pr_review_deps: PrReviewDeps | None = None,
) -> HandlerRegistry:
    """Instantiate enabled handlers from config, applying manifest overrides.

    `pr_review_deps` is required only when `[handlers.pr_review].enabled = true`
    AND the caller intends to actually dispatch (i.e. via the container). For
    inspection-only paths the dep is None and `pr_review` is silently skipped;
    `daeyeon-bot doctor` is the canonical liveness probe that exercises the
    full wiring.
    """
    registry = HandlerRegistry(routing=dict(config.routing))

    for name, entry in config.handlers.items():
        if not entry.enabled:
            continue
        if name == "pr_review" and pr_review_deps is None:
            continue
        record = instantiate_handler(name, entry, pr_review_deps=pr_review_deps)
        registry.register(record)

    return registry


def instantiate_handler(
    name: str,
    entry: HandlerEntry,
    *,
    pr_review_deps: PrReviewDeps | None = None,
) -> HandlerRecord:
    if name == "echo":
        manifest = _override_manifest(echo_handler.MANIFEST, entry)
        return HandlerRecord(
            name=name, manifest=manifest, instance=echo_handler.EchoHandler(manifest)
        )
    if name == "pr_review":
        if pr_review_deps is None:
            raise ConfigError(
                "pr_review handler requires PrReviewDeps; build via container.build()"
            )
        pr_entry = (
            entry
            if isinstance(entry, PrReviewHandlerEntry)
            else PrReviewHandlerEntry.model_validate(entry.model_dump())
        )
        manifest = _override_manifest(pr_review_handler.MANIFEST, pr_entry)
        kwargs: dict[str, Any] = {
            "manifest": manifest,
            "gh": pr_review_deps.gh,
            "persona_loader": pr_review_deps.persona_loader,
            "config": pr_entry,
            "github_username": pr_review_deps.github_username,
            "db": pr_review_deps.db,
        }
        if pr_review_deps.pause_guard is not None:
            kwargs["pause_guard"] = pr_review_deps.pause_guard
        instance = pr_review_handler.PrReviewHandler(**kwargs)
        return HandlerRecord(name=name, manifest=manifest, instance=instance)
    raise ConfigError(f"unknown handler in config: {name!r}")


@dataclass(frozen=True, slots=True)
class TriggerRecord:
    """An instantiated long-running trigger ready to be supervised."""

    name: str
    manifest: TriggerManifest
    instance: object  # actual Trigger — Protocol does not enforce at runtime.


@dataclass(frozen=True, slots=True)
class GhReviewRequestedDeps:
    """Runtime deps for the polling trigger.

    The trigger writes events directly via `storage_factory` (so the state
    UPSERT and the events INSERT commit in one TX). `gh` is a `GhCli` or
    a `FakeGh`; `clock` defaults to `SystemClock`. `pause_check` is the
    sync flag-file probe (`app.pause.is_paused`) bound to the configured
    `pause_flag_path`; on True the trigger sleeps one interval without
    hitting the GitHub API. `permanent_failure_reporter` records each
    `PermanentError` against `TriggerSupervisor` and returns True once the
    sliding window is tripped — the trigger then stops.
    """

    gh: Any
    storage_factory: StorageFactory
    github_username: str
    clock: Clock
    pause_check: Callable[[], bool]
    permanent_failure_reporter: gh_review_requested_trigger.PermanentFailureReporter


def build_trigger_registry(
    config: Config,
    *,
    gh_review_requested_deps: GhReviewRequestedDeps | None = None,
) -> list[TriggerRecord]:
    """Instantiate enabled live triggers from `config.triggers`.

    Triggers without an `enabled = true` entry are skipped silently.
    Inspection-only callers omit `gh_review_requested_deps`; the registry
    skips the trigger in that case (mirroring `pr_review` handler behavior).
    """
    out: list[TriggerRecord] = []
    for name, entry in config.triggers.items():
        if not entry.enabled:
            continue
        if name == "gh_review_requested" and gh_review_requested_deps is None:
            continue
        record = instantiate_trigger(
            name,
            entry,
            config=config,
            gh_review_requested_deps=gh_review_requested_deps,
        )
        if record is not None:
            out.append(record)
    return out


def instantiate_trigger(
    name: str,
    entry: object,
    *,
    config: Config,
    gh_review_requested_deps: GhReviewRequestedDeps | None = None,
) -> TriggerRecord | None:
    if name == "manual":
        # `manual` has no live loop — events arrive via the CLI. Skip.
        return None
    if name == "gh_review_requested":
        if gh_review_requested_deps is None:
            raise ConfigError(
                "gh_review_requested trigger requires GhReviewRequestedDeps;"
                " build via container.build()"
            )
        gh_entry = (
            entry
            if isinstance(entry, GhReviewRequestedTriggerEntry)
            else config.gh_review_requested_trigger_entry()
        )
        # Inherit the pr_review handler's allowed_repos so the search query
        # narrows to the same set the handler will accept. Two layers but
        # one source of truth — operators only edit `[handlers.pr_review]`.
        pr_review_entry = config.pr_review_handler_entry()
        search_extra_query = gh_review_requested_trigger.build_search_extra_query(
            pr_review_entry.allowed_repos
        )
        instance = gh_review_requested_trigger.GhReviewRequestedTrigger(
            gh=gh_review_requested_deps.gh,
            storage_factory=gh_review_requested_deps.storage_factory,
            github_username=gh_review_requested_deps.github_username,
            poll_interval_seconds=float(gh_entry.poll_interval_seconds),
            clock=gh_review_requested_deps.clock,
            pause_check=gh_review_requested_deps.pause_check,
            permanent_failure_reporter=gh_review_requested_deps.permanent_failure_reporter,
            search_extra_query=search_extra_query,
        )
        return TriggerRecord(
            name=name,
            manifest=gh_review_requested_trigger.MANIFEST,
            instance=instance,
        )
    raise ConfigError(f"unknown trigger in config: {name!r}")
