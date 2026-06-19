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
    CiTriageDeps,
    GhReviewRequestedDeps,
    HandlerRegistry,
    JiraAssignedDeps,
    JiraTriageDeps,
    PrReviewDeps,
    SlackCiAlertDeps,
    TriggerRecord,
    build_handler_registry,
    build_trigger_registry,
)
from daeyeon_bot.app.supervisor import TriggerSupervisor
from daeyeon_bot.core.errors import AuthError, ConfigError, QuotaError
from daeyeon_bot.core.time import Clock, SystemClock
from daeyeon_bot.handlers.pr_review import PauseGuard
from daeyeon_bot.infra import storage
from daeyeon_bot.infra.claude import ClaudeSession, make_real_factory
from daeyeon_bot.infra.gh_cli import GhCli
from daeyeon_bot.infra.host_resolver import HostResolver
from daeyeon_bot.infra.jira_client import FieldDiscovery, JiraClient, JiraIdentity
from daeyeon_bot.infra.logging import register_literal_secret
from daeyeon_bot.infra.loki import LokiClient
from daeyeon_bot.infra.oncall_wiki import OncallWiki
from daeyeon_bot.infra.persona_loader import PersonaLoader
from daeyeon_bot.infra.secrets import SecretsProvider
from daeyeon_bot.infra.slack import SlackClient
from daeyeon_bot.infra.ssh_logs import SshLogClient
from daeyeon_bot.infra.ssw_bundle import SswBundleClient

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
    # Feature 002 overrides.
    jira: object | None = None  # JiraClient or a FakeJira
    loki: object | None = None  # LokiClient or a FakeLoki
    ssh: object | None = None  # SshLogClient or a FakeSshLogs
    ssw_bundle: object | None = None  # SswBundleClient or a fake
    jira_identity: JiraIdentity | None = None
    field_discovery: FieldDiscovery | None = None
    secrets_provider: SecretsProvider | None = None
    project_root: Any = None  # Path | None — project root for ssw_bundle path guard
    # Feature 003 overrides.
    slack: object | None = None  # SlackClient or a FakeSlack
    oncall_wiki: object | None = None  # OncallWiki or a fake


async def build(
    config: Config,
    db: aiosqlite.Connection,
    *,
    oauth_token: str | None = None,
    secrets_provider: SecretsProvider | None = None,
    overrides: ContainerOverrides | None = None,
) -> Container:
    """Wire concrete dependencies. `db` must already be opened + migrated.

    Async because resolving `github.username` may require one boot-time
    `gh api /user` round-trip when the operator has not pinned it in config.

    `secrets_provider` (when supplied) lets feature handlers that need named
    secrets (jira_triage's JIRA_USER/JIRA_API_TOKEN/SSW_AUTOMATION_PASSWORD)
    resolve them via the same provider chain the OAuth token uses. Tests
    skip it by passing FakeJira via `overrides.jira`.
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

    # Feature 002: Jira triage handler + trigger.
    jira_triage_deps, jira_assigned_deps = await _build_jira_deps(
        config=config,
        db=db,
        clock=clock,
        overrides=overrides,
        persona_loader=persona_loader,
        oauth_token=oauth_token,
        secrets_provider=secrets_provider,
    )

    # Feature 003: CI Monitor / OnCall triage handler + slack_ci_alert trigger.
    # One Slack client, shared (handler posts, trigger reads); one auth.test probe.
    slack_client = await _build_slack_client(
        config=config, overrides=overrides, secrets_provider=secrets_provider
    )
    ci_triage_deps = await _build_ci_triage_deps(
        config=config,
        db=db,
        overrides=overrides,
        gh=gh,
        persona_loader=persona_loader,
        slack_client=slack_client,
    )
    slack_ci_alert_deps = _build_slack_ci_alert_deps(
        config=config, slack_client=slack_client, clock=clock
    )

    triggers = build_trigger_registry(
        config,
        gh_review_requested_deps=gh_trigger_deps,
        jira_assigned_deps=jira_assigned_deps,
        slack_ci_alert_deps=slack_ci_alert_deps,
    )

    return Container(
        config=config,
        clock=clock,
        db=db,
        handlers=build_handler_registry(
            config,
            pr_review_deps=pr_deps,
            jira_triage_deps=jira_triage_deps,
            ci_triage_deps=ci_triage_deps,
        ),
        triggers=tuple(triggers),
        claude_session_factory=factory,
        gh=gh,
        persona_loader=persona_loader,
        github_username=github_username,
    )


def _jira_triage_enabled(config: Config) -> bool:
    entry = config.handlers.get("jira_triage")
    return entry is not None and entry.enabled


def _jira_assigned_enabled(config: Config) -> bool:
    entry = config.triggers.get("jira_assigned")
    return entry is not None and entry.enabled


def _ci_triage_enabled(config: Config) -> bool:
    entry = config.handlers.get("ci_triage")
    return entry is not None and entry.enabled


def _slack_ci_alert_enabled(config: Config) -> bool:
    entry = config.triggers.get("slack_ci_alert")
    return entry is not None and entry.enabled


async def _build_slack_client(
    *,
    config: Config,
    overrides: ContainerOverrides,
    secrets_provider: SecretsProvider | None,
) -> object | None:
    """Build the shared SlackClient when the ci_triage handler OR the
    slack_ci_alert trigger is enabled (the handler posts, the trigger reads —
    same bot token, one `auth.test` probe). Returns None when neither is enabled.
    """
    if not (_ci_triage_enabled(config) or _slack_ci_alert_enabled(config)):
        return None
    if overrides.slack is not None:
        return overrides.slack
    effective_secrets = overrides.secrets_provider or secrets_provider
    if effective_secrets is None:
        raise RuntimeError(
            "container.build: ci_triage / slack_ci_alert require a secrets_provider"
            " (production) OR a slack override (test path)"
        )
    try:
        token = effective_secrets.load_secret("slack_bot_token")
    except AuthError as exc:
        # Distinguish "never configured" from "rejected" (doctor clarity).
        raise ConfigError(
            "slack_bot_token not configured; set it or disable"
            " [handlers.ci_triage] / [triggers.slack_ci_alert]"
        ) from exc
    register_literal_secret(token)  # defense-in-depth redaction
    # Post allowlist = watched channels + dry_run (empties dropped — the trigger
    # only reads, so an empty dry_run when only the trigger is on is fine).
    channels = tuple(config.slack_ci_alert_trigger_entry().channels)
    dry_run = config.ci_triage_handler_entry().dry_run_channel
    allowed = frozenset(c for c in (*channels, dry_run) if c)
    client = SlackClient(
        token=token,
        allowed_post_channels=allowed,
        api_base=config.slack.api_base,
        timeout_s=float(config.slack.timeout_seconds),
    )
    await client.auth_test()  # boot probe — AuthError (rejected) → exit 78
    return client


async def _build_ci_triage_deps(
    *,
    config: Config,
    db: aiosqlite.Connection,
    overrides: ContainerOverrides,
    gh: object | None,
    persona_loader: PersonaLoader | None,
    slack_client: object | None,
) -> CiTriageDeps | None:
    """Feature 003 handler deps, built only when `[handlers.ci_triage].enabled`.
    The Slack client is built once by `_build_slack_client` and shared with the
    trigger. Runs the OnCall-wiki `ls_remote` boot probe (fail loud at boot)."""
    if not _ci_triage_enabled(config):
        return None

    entry = config.ci_triage_handler_entry()
    if not entry.dry_run_channel:
        raise ConfigError("ci_triage enabled but [handlers.ci_triage].dry_run_channel is unset")
    if slack_client is None:
        raise RuntimeError("container.build: ci_triage enabled but no Slack client was built")

    # OnCall wiki clone.
    wiki_client: Any
    if overrides.oncall_wiki is not None:
        wiki_client = overrides.oncall_wiki
    else:
        project_root: Path | None = overrides.project_root
        clone_path = Path(config.oncall_wiki.clone_path).expanduser()  # noqa: ASYNC240
        if not clone_path.is_absolute() and project_root is not None:
            clone_path = project_root / clone_path
        wiki_client = OncallWiki(
            clone_path=clone_path,
            known_hosts_path=config.state_dir_path / config.oncall_wiki.known_hosts_path,
            remote_url=config.oncall_wiki.remote_url,
            project_root=project_root,
            allow_external=config.oncall_wiki.allow_external,
        )
        await wiki_client.ls_remote()  # boot probe — ConfigError/AuthError → exit 78

    gh_client = overrides.gh or gh or GhCli(timeout_seconds=config.github.gh_call_timeout_seconds)
    loader = (
        overrides.persona_loader
        or persona_loader
        or PersonaLoader(skills_root=_resolve_skills_root(config))
    )
    pause_guard = overrides.pause_guard or _make_pause_guard(config)
    return CiTriageDeps(
        slack=slack_client,
        gh=gh_client,
        oncall_wiki=wiki_client,
        persona_loader=loader,
        db=db,
        pause_guard=pause_guard,
    )


def _build_slack_ci_alert_deps(
    *,
    config: Config,
    slack_client: object | None,
    clock: Clock,
) -> SlackCiAlertDeps | None:
    """Feature 003 trigger deps, built only when `[triggers.slack_ci_alert].enabled`.
    Reuses the shared SlackClient (read side)."""
    if not _slack_ci_alert_enabled(config):
        return None
    if slack_client is None:
        raise RuntimeError("container.build: slack_ci_alert enabled but no Slack client was built")

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
                conn, trigger_name="slack_ci_alert", reason=reason, at=clock.now()
            )

    return SlackCiAlertDeps(
        slack=slack_client,
        storage_factory=_storage_factory,
        clock=clock,
        pause_check=_pause_check,
        permanent_failure_reporter=_report_permanent_failure,
    )


async def _build_jira_deps(  # noqa: PLR0912, PLR0915 — composition root branches by config knobs
    *,
    config: Config,
    db: aiosqlite.Connection,
    clock: Clock,
    overrides: ContainerOverrides,
    persona_loader: PersonaLoader | None,
    oauth_token: str | None,
    secrets_provider: SecretsProvider | None = None,
) -> tuple[JiraTriageDeps | None, JiraAssignedDeps | None]:
    """Construct the feature-002 deps if the handler/trigger is enabled.

    Both share a single JiraClient (one boot-time auth probe + field
    discovery), so we build them together.
    """
    triage_enabled = _jira_triage_enabled(config)
    trigger_enabled = _jira_assigned_enabled(config)
    if not (triage_enabled or trigger_enabled):
        return (None, None)

    # Resolve credentials. Tests may inject a FakeJira via overrides.jira;
    # in that case we skip the real httpx + secrets path entirely.
    # Production: secrets_provider was built by lifecycle and passed in.
    jira_client: Any
    effective_secrets = overrides.secrets_provider or secrets_provider
    if overrides.jira is not None:
        jira_client = overrides.jira
    else:
        if effective_secrets is None:
            raise RuntimeError(
                "container.build: jira_triage / jira_assigned require a"
                " secrets_provider (production path) OR a jira override"
                " (test path)"
            )
        del oauth_token  # not used here — jira has its own (user,token).
        user = effective_secrets.load_secret("jira_user")
        token = effective_secrets.load_secret("jira_api_token")
        jira_client = JiraClient(
            base_url=config.jira.base_url,
            user=user,
            token=token,
            timeout_s=float(config.jira.timeout_seconds),
        )

    # Boot-time probes.
    identity: JiraIdentity = (
        overrides.jira_identity
        if overrides.jira_identity is not None
        else await jira_client.myself()
    )
    if overrides.field_discovery is not None:
        field_discovery: FieldDiscovery = overrides.field_discovery
    else:
        triage_entry = config.jira_triage_handler_entry()
        candidates: tuple[str, ...] = ("TC Failure", "Bug")
        if config.jira.issuetype_override:
            candidates = (config.jira.issuetype_override, "Bug")
        field_discovery = await jira_client.discover_fields(
            project_keys=list(triage_entry.allowed_projects) or ["SSWCI"],
            issuetype_candidates=candidates,
        )

    # Triage handler deps.
    triage_deps: JiraTriageDeps | None = None
    if triage_enabled:
        loki_client = overrides.loki or LokiClient(
            base_url=config.loki.base_url,
            timeout_s=float(config.loki.timeout_seconds),
            per_stream_max_bytes=config.loki.per_stream_max_bytes,
        )
        if overrides.ssh is not None:
            ssh_client: Any = overrides.ssh
        else:
            if effective_secrets is None:
                raise RuntimeError(
                    "container.build: jira_triage needs secrets_provider for"
                    " SSW_AUTOMATION_PASSWORD"
                )
            ssh_password = effective_secrets.load_secret("ssw_automation_password")
            triage_entry = config.jira_triage_handler_entry()
            ssh_client = SshLogClient(
                username="automation",
                password=ssh_password,
                known_hosts_path=config.state_dir_path / triage_entry.ssh_known_hosts_path,
                max_file_bytes=triage_entry.ssh_max_file_bytes,
            )
        if overrides.ssw_bundle is not None:
            ssw_client: Any = overrides.ssw_bundle
        else:
            triage_entry = config.jira_triage_handler_entry()
            project_root: Path | None = overrides.project_root
            # Boot-time path resolution — `expanduser`/`is_absolute` are
            # pure metadata ops, not filesystem I/O; ASYNC240's anyio.path
            # suggestion doesn't apply.
            clone_path = Path(triage_entry.ssw_bundle_path).expanduser()  # noqa: ASYNC240
            if not clone_path.is_absolute() and project_root is not None:
                clone_path = project_root / clone_path
            ssw_client = SswBundleClient(
                clone_path=clone_path,
                project_root=project_root,
                allow_external=triage_entry.allow_external_ssw_bundle,
            )

        # Order of precedence: explicit overrides → reuse pr_review's loader
        # if both handlers are enabled → default. The first arm catches the
        # jira-only test path where `pr_review_enabled=False` so the outer
        # `persona_loader` local stayed None.
        loader = overrides.persona_loader or persona_loader or PersonaLoader()
        pause_guard = overrides.pause_guard or _make_pause_guard(config)
        triage_deps = JiraTriageDeps(
            jira=jira_client,
            loki=loki_client,
            ssh=ssh_client,
            ssw_bundle=ssw_client,
            host_resolver_factory=HostResolver,
            persona_loader=loader,
            db=db,
            jira_identity=identity,
            field_discovery=field_discovery,
            pause_guard=pause_guard,
        )

    # Trigger deps.
    trigger_deps: JiraAssignedDeps | None = None
    if trigger_enabled:
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
                    trigger_name="jira_assigned",
                    reason=reason,
                    at=clock.now(),
                )

        trigger_deps = JiraAssignedDeps(
            jira=jira_client,
            storage_factory=_storage_factory,
            jira_account_id=identity.account_id,
            issuetype_name=(config.jira.issuetype_override or field_discovery.issuetype_name),
            team_field_id=field_discovery.team_field_id,
            clock=clock,
            pause_check=_pause_check,
            permanent_failure_reporter=_report_permanent_failure,
        )

    return (triage_deps, trigger_deps)


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
