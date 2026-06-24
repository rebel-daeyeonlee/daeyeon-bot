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
    CiTriageHandlerEntry,
    Config,
    GhReviewRequestedTriggerEntry,
    HandlerEntry,
    JiraAssignedTriggerEntry,
    JiraTriageHandlerEntry,
    PrReviewHandlerEntry,
    SlackCiAlertTriggerEntry,
)
from daeyeon_bot.core.errors import ConfigError
from daeyeon_bot.core.manifest import HandlerManifest, TriggerManifest
from daeyeon_bot.core.time import Clock
from daeyeon_bot.handlers import ci_triage as ci_triage_handler
from daeyeon_bot.handlers import echo as echo_handler
from daeyeon_bot.handlers import jira_triage as jira_triage_handler
from daeyeon_bot.handlers import pr_review as pr_review_handler
from daeyeon_bot.handlers.pr_review import PauseGuard
from daeyeon_bot.infra.jira_client import FieldDiscovery, JiraIdentity
from daeyeon_bot.infra.persona_loader import PersonaLoader
from daeyeon_bot.triggers import gh_review_requested as gh_review_requested_trigger
from daeyeon_bot.triggers import jira_assigned as jira_assigned_trigger
from daeyeon_bot.triggers import slack_ci_alert as slack_ci_alert_trigger
from daeyeon_bot.triggers.gh_review_requested import StorageFactory


@dataclass(frozen=True, slots=True)
class HandlerRecord:
    """An instantiated handler ready to dispatch."""

    name: str
    manifest: HandlerManifest
    instance: object  # actual Handler — typing.Protocol does not enforce at runtime here.


@dataclass(frozen=True, slots=True)
class JiraTriageDeps:
    """Runtime dependencies the `jira_triage` handler can't fabricate itself.

    Inspection-only callers omit these; the registry skips the handler in
    that case (mirrors PrReviewDeps).
    """

    jira: Any
    loki: Any
    ssh: Any
    ssw_bundle: Any
    host_resolver_factory: Callable[[], Any]
    persona_loader: PersonaLoader
    db: Any
    jira_identity: JiraIdentity
    field_discovery: FieldDiscovery
    pause_guard: PauseGuard | None = None


@dataclass(frozen=True, slots=True)
class CiTriageDeps:
    """Runtime deps for the `ci_triage` handler (feature 003). Inspection-only
    callers omit these and the registry skips the handler (mirrors PrReviewDeps)."""

    slack: Any
    gh: Any
    oncall_wiki: Any
    persona_loader: PersonaLoader
    db: Any
    loki: Any = None
    jira: Any = None  # JiraClient | None — P2/P4 ticket search (best-effort)
    linear: Any = None  # LinearClient | None — P2/P4 ticket search (best-effort)
    pause_guard: PauseGuard | None = None


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
    jira_triage_deps: JiraTriageDeps | None = None,
    ci_triage_deps: CiTriageDeps | None = None,
) -> HandlerRegistry:
    """Instantiate enabled handlers from config, applying manifest overrides.

    `pr_review_deps` / `jira_triage_deps` are required only when the
    corresponding handler is `enabled = true` AND the caller intends to
    actually dispatch (i.e. via the container). For inspection-only
    paths the dep is None and the handler is silently skipped.
    """
    registry = HandlerRegistry(routing=dict(config.routing))

    for name, entry in config.handlers.items():
        if not entry.enabled:
            continue
        if name == "pr_review" and pr_review_deps is None:
            continue
        if name == "jira_triage" and jira_triage_deps is None:
            continue
        if name == "ci_triage" and ci_triage_deps is None:
            continue
        record = instantiate_handler(
            name,
            entry,
            config=config,
            pr_review_deps=pr_review_deps,
            jira_triage_deps=jira_triage_deps,
            ci_triage_deps=ci_triage_deps,
        )
        registry.register(record)

    return registry


def instantiate_handler(
    name: str,
    entry: HandlerEntry,
    *,
    config: Config | None = None,
    pr_review_deps: PrReviewDeps | None = None,
    jira_triage_deps: JiraTriageDeps | None = None,
    ci_triage_deps: CiTriageDeps | None = None,
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
    if name == "jira_triage":
        if jira_triage_deps is None:
            raise ConfigError(
                "jira_triage handler requires JiraTriageDeps; build via container.build()"
            )
        if config is None:
            raise ConfigError(
                "jira_triage handler requires config (for LokiConfig); pass via build_handler_registry"
            )
        jira_entry = (
            entry
            if isinstance(entry, JiraTriageHandlerEntry)
            else JiraTriageHandlerEntry.model_validate(entry.model_dump())
        )
        manifest = _override_manifest(jira_triage_handler.MANIFEST, jira_entry)
        jt_kwargs: dict[str, Any] = {
            "manifest": manifest,
            "jira": jira_triage_deps.jira,
            "loki": jira_triage_deps.loki,
            "ssh": jira_triage_deps.ssh,
            "ssw_bundle": jira_triage_deps.ssw_bundle,
            "host_resolver_factory": jira_triage_deps.host_resolver_factory,
            "persona_loader": jira_triage_deps.persona_loader,
            "config": jira_entry,
            "loki_config": config.loki,
            "db": jira_triage_deps.db,
            "jira_identity": jira_triage_deps.jira_identity,
            "field_discovery": jira_triage_deps.field_discovery,
        }
        if jira_triage_deps.pause_guard is not None:
            jt_kwargs["pause_guard"] = jira_triage_deps.pause_guard
        instance = jira_triage_handler.JiraTriageHandler(**jt_kwargs)
        return HandlerRecord(name=name, manifest=manifest, instance=instance)
    if name == "ci_triage":
        if ci_triage_deps is None:
            raise ConfigError(
                "ci_triage handler requires CiTriageDeps; build via container.build()"
            )
        ci_entry = (
            entry
            if isinstance(entry, CiTriageHandlerEntry)
            else CiTriageHandlerEntry.model_validate(entry.model_dump())
        )
        manifest = _override_manifest(ci_triage_handler.MANIFEST, ci_entry)
        ct_kwargs: dict[str, Any] = {
            "manifest": manifest,
            "slack": ci_triage_deps.slack,
            "gh": ci_triage_deps.gh,
            "oncall_wiki": ci_triage_deps.oncall_wiki,
            "persona_loader": ci_triage_deps.persona_loader,
            "config": ci_entry,
            "db": ci_triage_deps.db,
            "loki": ci_triage_deps.loki,
            "jira": ci_triage_deps.jira,
            "linear": ci_triage_deps.linear,
        }
        if ci_triage_deps.pause_guard is not None:
            ct_kwargs["pause_guard"] = ci_triage_deps.pause_guard
        instance = ci_triage_handler.CiTriageHandler(**ct_kwargs)
        return HandlerRecord(name=name, manifest=manifest, instance=instance)
    raise ConfigError(f"unknown handler in config: {name!r}")


@dataclass(frozen=True, slots=True)
class TriggerRecord:
    """An instantiated long-running trigger ready to be supervised."""

    name: str
    manifest: TriggerManifest
    instance: object  # actual Trigger — Protocol does not enforce at runtime.


@dataclass(frozen=True, slots=True)
class JiraAssignedDeps:
    """Runtime deps for the `jira_assigned` polling trigger.

    Mirrors GhReviewRequestedDeps. `jira` is a JiraClient or FakeJira;
    `jira_account_id` is `JiraIdentity.account_id` (resolved at boot);
    `issuetype_name` + `team_field_id` come from FieldDiscovery (also
    resolved at boot).
    """

    jira: Any
    storage_factory: jira_assigned_trigger.StorageFactory
    jira_account_id: str
    issuetype_name: str
    team_field_id: str
    clock: Clock
    pause_check: Callable[[], bool]
    permanent_failure_reporter: jira_assigned_trigger.PermanentFailureReporter


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


@dataclass(frozen=True, slots=True)
class SlackCiAlertDeps:
    """Runtime deps for the `slack_ci_alert` polling trigger (feature 003).

    `slack` is a SlackClient (shared with the ci_triage handler) or a FakeSlack.
    The trigger persists events directly via `storage_factory` (cursor advance +
    events INSERT in one tx per candidate). `pause_check` skips the poll on PAUSE;
    `permanent_failure_reporter` quarantines after the failure window trips.
    """

    slack: Any
    storage_factory: slack_ci_alert_trigger.StorageFactory
    clock: Clock
    pause_check: Callable[[], bool]
    permanent_failure_reporter: slack_ci_alert_trigger.PermanentFailureReporter


def build_trigger_registry(
    config: Config,
    *,
    gh_review_requested_deps: GhReviewRequestedDeps | None = None,
    jira_assigned_deps: JiraAssignedDeps | None = None,
    slack_ci_alert_deps: SlackCiAlertDeps | None = None,
) -> list[TriggerRecord]:
    """Instantiate enabled live triggers from `config.triggers`."""
    out: list[TriggerRecord] = []
    for name, entry in config.triggers.items():
        if not entry.enabled:
            continue
        if name == "gh_review_requested" and gh_review_requested_deps is None:
            continue
        if name == "jira_assigned" and jira_assigned_deps is None:
            continue
        if name == "slack_ci_alert" and slack_ci_alert_deps is None:
            continue
        record = instantiate_trigger(
            name,
            entry,
            config=config,
            gh_review_requested_deps=gh_review_requested_deps,
            jira_assigned_deps=jira_assigned_deps,
            slack_ci_alert_deps=slack_ci_alert_deps,
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
    jira_assigned_deps: JiraAssignedDeps | None = None,
    slack_ci_alert_deps: SlackCiAlertDeps | None = None,
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
            review_self=pr_review_entry.review_self,
        )
        return TriggerRecord(
            name=name,
            manifest=gh_review_requested_trigger.MANIFEST,
            instance=instance,
        )
    if name == "jira_assigned":
        if jira_assigned_deps is None:
            raise ConfigError(
                "jira_assigned trigger requires JiraAssignedDeps; build via container.build()"
            )
        ja_entry = (
            entry
            if isinstance(entry, JiraAssignedTriggerEntry)
            else config.jira_assigned_trigger_entry()
        )
        jt_entry = config.jira_triage_handler_entry()
        instance = jira_assigned_trigger.JiraAssignedTrigger(
            jira=jira_assigned_deps.jira,
            storage_factory=jira_assigned_deps.storage_factory,
            jira_account_id=jira_assigned_deps.jira_account_id,
            allowed_projects=tuple(jt_entry.allowed_projects),
            team_name=ja_entry.team_name,
            team_field_id=jira_assigned_deps.team_field_id,
            issuetype_name=jira_assigned_deps.issuetype_name,
            poll_interval_seconds=float(ja_entry.poll_interval_seconds),
            max_per_cycle=ja_entry.max_per_cycle,
            clock=jira_assigned_deps.clock,
            pause_check=jira_assigned_deps.pause_check,
            permanent_failure_reporter=jira_assigned_deps.permanent_failure_reporter,
        )
        return TriggerRecord(
            name=name,
            manifest=jira_assigned_trigger.MANIFEST,
            instance=instance,
        )
    if name == "slack_ci_alert":
        if slack_ci_alert_deps is None:
            raise ConfigError(
                "slack_ci_alert trigger requires SlackCiAlertDeps; build via container.build()"
            )
        sca_entry = (
            entry
            if isinstance(entry, SlackCiAlertTriggerEntry)
            else config.slack_ci_alert_trigger_entry()
        )
        instance = slack_ci_alert_trigger.SlackCiAlertTrigger(
            slack=slack_ci_alert_deps.slack,
            storage_factory=slack_ci_alert_deps.storage_factory,
            channels=tuple(sca_entry.channels),
            poll_interval_seconds=float(sca_entry.poll_interval_seconds),
            max_per_cycle=sca_entry.max_per_cycle,
            staleness_seconds=float(sca_entry.staleness_seconds),
            clock=slack_ci_alert_deps.clock,
            pause_check=slack_ci_alert_deps.pause_check,
            permanent_failure_reporter=slack_ci_alert_deps.permanent_failure_reporter,
        )
        return TriggerRecord(
            name=name,
            manifest=slack_ci_alert_trigger.MANIFEST,
            instance=instance,
        )
    raise ConfigError(f"unknown trigger in config: {name!r}")
