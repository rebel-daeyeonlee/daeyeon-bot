"""Configuration loader (pydantic-settings, TOML + .env).

The config object is the *whole* configuration surface, validated once at boot.
Trigger / handler / routing sections are dictionaries keyed by name.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSection(BaseModel):
    state_dir: str = "~/.daeyeon-bot"
    shutdown_budget_seconds: int = 180


class LoggingSection(BaseModel):
    level: str = "INFO"
    format: str = "json"


class RetentionSection(BaseModel):
    events_days: int = 90
    runs_days: int = 30
    runs_keep_per_handler: int = 10
    dedup_default_ttl_days: int = 7
    backup_keep: int = 5
    gh_state_dormant_days: int = 90
    # Dormant jira_assigned_state rows (in_pending_set=0) are pruned after
    # this many days. Feature 002.
    jira_state_dormant_days: int = 180
    # Outbox rows that landed in `status='dead_letter'` are pruned ahead
    # of the generic `events_days` window so they don't accumulate
    # indefinitely (the FK-aware `_prune_events` only deletes outbox rows
    # once the parent event is past `events_days`). Operator-tunable;
    # default 30 days keeps a month of forensic history.
    dead_letter_days: int = 30


class RateLimitDefaults(BaseModel):
    global_per_hour: int = 30
    global_per_day: int = 200
    handler_per_hour: int = 10


class RateLimitSection(BaseModel):
    # Token-bucket gate the dispatcher consults before each claim. Capacity
    # is the burst budget; refill_per_sec is the steady-state rate. Defaults
    # are a soft 60/min cap with full-minute burst headroom — see migration
    # 003 and OPTIMIZATION_PLAN §A3.
    claude_call_capacity: float = 60.0
    claude_call_refill_per_sec: float = 1.0
    defaults: RateLimitDefaults = Field(default_factory=RateLimitDefaults)


class SecretsSection(BaseModel):
    provider: str = "keychain"
    keychain_service: str = "daeyeon-bot"
    keychain_account: str = "oauth_token"
    file_path: str = "/etc/daeyeon-bot/oauth_token"


class ClaudeSection(BaseModel):
    model: str = "claude-opus-4-7"
    default_system_prompt: str = "You are daeyeon's helpful assistant."


class GitHubConfig(BaseModel):
    """GitHub integration knobs. Auth itself flows through `gh` CLI."""

    model_config = ConfigDict(extra="forbid")
    username: str = ""
    gh_call_timeout_seconds: int = 30


class JiraConfig(BaseModel):
    """Jira REST integration knobs. Auth = basic (JIRA_USER, JIRA_API_TOKEN)."""

    model_config = ConfigDict(extra="forbid")
    base_url: str = "https://rbln.atlassian.net/"
    # Override for the autodiscovered regression-failure issuetype name.
    # Leave empty to autodiscover against {"TC Failure", "Bug"} at boot.
    issuetype_override: str = ""
    timeout_seconds: int = 30


class LokiConfig(BaseModel):
    """Loki HTTP query knobs. Cluster-internal, no auth."""

    model_config = ConfigDict(extra="forbid")
    base_url: str = "http://loki.ssw.rbln.in"
    per_stream_max_bytes: int = 1_048_576  # 1 MB
    timeout_seconds: int = 30
    # `rsmd [cdp]` FW console dumps land in the syslog logtype but Alloy
    # parses their RFC 3164 timestamps as KST when they were actually UTC,
    # which shifts ingestion time by -9h. Widening the syslog query window
    # by this many extra hours on each side recovers the shifted entries.
    # See ssw-debugger log-analysis SKILL.md for the upstream bug.
    syslog_window_extra_hours: int = 12
    # LogQL templates for kernel/syslog streams. `{host}` is substituted at
    # query time with the hostname-by-name. The label schema is the canonical
    # SSW Loki shape — `hostname` + `logtype` — documented in
    # ssw-debugger/.../skills/log-analysis/SKILL.md. The old `job` / `filename`
    # combo matched zero streams in production.
    kernel_query_template: str = '{hostname="{host}", logtype="kernel"}'
    syslog_query_template: str = '{hostname="{host}", logtype="syslog"}'


class TriggerEntry(BaseModel):
    """Runtime override for a trigger. Extra keys are passed to the trigger constructor."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = True


class GhReviewRequestedTriggerEntry(TriggerEntry):
    """Typed view of `[triggers.gh_review_requested]`."""

    poll_interval_seconds: int = 300


class JiraAssignedTriggerEntry(TriggerEntry):
    """Typed view of `[triggers.jira_assigned]`."""

    poll_interval_seconds: int = 300
    max_per_cycle: int = 200
    # Also match tickets assigned to this Atlassian Team. Empty string
    # disables team match (assignee-only mode).
    team_name: str = "DevOps"


class HandlerEntry(BaseModel):
    """Runtime override for a handler. Mirrors HandlerManifest fields."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    idempotent: bool | None = None
    dedup_ttl_seconds: int | None = None
    side_effect_key: str | None = None
    concurrency: int | None = None
    accepts: list[str] | None = None


class SizeBudget(BaseModel):
    """PR-diff size budget enforced before calling Claude."""

    model_config = ConfigDict(extra="forbid")
    max_lines: int = 1000
    max_files: int = 50


class PrReviewHandlerEntry(HandlerEntry):
    """Typed view of `[handlers.pr_review]`."""

    persona_skill: str | None = None
    min_persona_chars: int = 200
    # Where to look for `<persona_skill>/SKILL.md`. Defaults to
    # `~/.claude/skills` (the standard Claude Code convention). Override
    # to point at a repo-local skills dir like `.claude/skills` so the
    # bundled persona works without an extra symlink step.
    skills_root: str | None = None
    size_budget: SizeBudget = Field(default_factory=SizeBudget)
    # Glob allowlist of `owner/repo` patterns the bot is permitted to review.
    # Empty list = no filter (any repo where the operator is review-requested
    # triggers a review). Non-empty list applies in two layers:
    #   1) trigger search query — when expressible, an `OR`-joined filter
    #      (`repo:a/b OR user:c`) cuts traffic at the GitHub side;
    #   2) handler — fnmatch defense-in-depth before any `gh.pr_get` call.
    # Globs accepted: `owner/repo`, `owner/*`. Anything else (e.g. `*foo*`)
    # falls back to handler-only filtering.
    allowed_repos: list[str] = Field(default_factory=list)
    # When true, also review the operator's OWN open PRs (discovered via an
    # `author:<operator>` search in the trigger). Self-authored reviews are
    # always submitted as GitHub `COMMENT` events — GitHub rejects a
    # self-`APPROVE` with HTTP 422. Pairs best with a non-empty `allowed_repos`:
    # with an empty allowlist this scoops up every open PR you have across all
    # of GitHub. Default false preserves the `skipped_self_authored` behavior.
    review_self: bool = False


class JiraTriageHandlerEntry(HandlerEntry):
    """Typed view of `[handlers.jira_triage]`. Feature 002."""

    # Allowed Jira project keys. Empty list = auto-trigger never fires
    # (defense in depth).
    allowed_projects: list[str] = Field(default_factory=lambda: ["SSWCI"])
    # Persona — same shape as pr_review.
    persona_skill: str | None = None
    min_persona_chars: int = 200
    skills_root: str | None = None
    # Per-event wall-clock cap covering all stages.
    timeout_seconds: int = 600
    # Optional per-project timeout override. Map of Jira project key →
    # seconds. Project keys not listed use `timeout_seconds`. Useful when
    # one project's ssw-bundle clone is materially slower (large
    # submodule tree, slow network mount) than the rest.
    timeout_overrides_seconds: dict[str, int] = Field(default_factory=dict)

    def timeout_for_project(self, project_key: str) -> int:
        """Resolve the wall-clock budget for an event from project `project_key`."""
        return self.timeout_overrides_seconds.get(project_key, self.timeout_seconds)

    # Project-root-relative path for the dedicated ssw-bundle clone.
    ssw_bundle_path: str = "var/ssw-bundle"
    allow_external_ssw_bundle: bool = False
    # SSH knobs.
    ssh_known_hosts_path: str = "jira_triage_known_hosts"
    ssh_max_file_bytes: int = 10_485_760  # 10 MB
    ssh_fetch_globs: list[str] = Field(
        default_factory=lambda: ["output.xml", "dmesg.log", "console.log"]
    )
    # Custom-field IDs override (leave empty for autodiscovery).
    branch_field_id: str = ""
    commit_field_id: str = ""
    team_field_id: str = ""


class Config(BaseSettings):
    # `extra="forbid"` so a typo like `[handlrs.pr_review]` raises at boot
    # instead of silently dropping the section. The two leaf entries
    # (`TriggerEntry`, `HandlerEntry`) keep `extra="allow"` because they
    # pass arbitrary kwargs through to constructors.
    model_config = SettingsConfigDict(
        env_prefix="DAEYEON_BOT__",
        env_nested_delimiter="__",
        extra="forbid",
    )

    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    retention: RetentionSection = Field(default_factory=RetentionSection)
    ratelimit: RateLimitSection = Field(default_factory=RateLimitSection)
    secrets: SecretsSection = Field(default_factory=SecretsSection)
    claude: ClaudeSection = Field(default_factory=ClaudeSection)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    loki: LokiConfig = Field(default_factory=LokiConfig)

    triggers: dict[str, TriggerEntry] = Field(default_factory=dict)
    handlers: dict[str, HandlerEntry] = Field(default_factory=dict)
    routing: dict[str, list[str]] = Field(default_factory=dict)

    raw: dict[str, Any] = Field(default_factory=dict)

    def gh_review_requested_trigger_entry(self) -> GhReviewRequestedTriggerEntry:
        """Typed view of `[triggers.gh_review_requested]` (with defaults)."""
        raw = self.triggers.get("gh_review_requested")
        if raw is None:
            return GhReviewRequestedTriggerEntry()
        return GhReviewRequestedTriggerEntry.model_validate(raw.model_dump())

    def pr_review_handler_entry(self) -> PrReviewHandlerEntry:
        """Typed view of `[handlers.pr_review]` (with defaults)."""
        raw = self.handlers.get("pr_review")
        if raw is None:
            return PrReviewHandlerEntry()
        return PrReviewHandlerEntry.model_validate(raw.model_dump())

    def jira_assigned_trigger_entry(self) -> JiraAssignedTriggerEntry:
        """Typed view of `[triggers.jira_assigned]` (with defaults). Feature 002."""
        raw = self.triggers.get("jira_assigned")
        if raw is None:
            return JiraAssignedTriggerEntry()
        return JiraAssignedTriggerEntry.model_validate(raw.model_dump())

    def jira_triage_handler_entry(self) -> JiraTriageHandlerEntry:
        """Typed view of `[handlers.jira_triage]` (with defaults). Feature 002."""
        raw = self.handlers.get("jira_triage")
        if raw is None:
            return JiraTriageHandlerEntry()
        return JiraTriageHandlerEntry.model_validate(raw.model_dump())

    @property
    def state_dir_path(self) -> Path:
        return Path(self.runtime.state_dir).expanduser()

    @property
    def db_path(self) -> Path:
        return self.state_dir_path / "state.db"

    @property
    def pause_flag_path(self) -> Path:
        return self.state_dir_path / "PAUSE"

    @property
    def pidfile_path(self) -> Path:
        return self.state_dir_path / "daeyeon-bot.pid"


def resolve_config_path(explicit: str | None) -> Path | None:
    """Public: resolve which config.toml `load()` would use, or None for defaults.

    Order: explicit `--config` > `DAEYEON_BOT_CONFIG` env > `./config.toml`.
    Used by `cli/ops.py:doctor` to surface "using defaults" when nothing
    resolves, so a first-time operator doesn't get silent fallback behavior.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("DAEYEON_BOT_CONFIG")
    if env:
        return Path(env).expanduser()
    default = Path.cwd() / "config.toml"
    return default if default.exists() else None


def load(path: str | None = None) -> Config:
    """Load config from TOML (if present) and environment overrides."""
    config_path = resolve_config_path(path)
    if config_path and config_path.is_file():
        with config_path.open("rb") as fp:
            data = tomllib.load(fp)
        return Config(**data, raw=data)
    return Config()
