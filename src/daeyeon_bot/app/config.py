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


class TriggerEntry(BaseModel):
    """Runtime override for a trigger. Extra keys are passed to the trigger constructor."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = True


class GhReviewRequestedTriggerEntry(TriggerEntry):
    """Typed view of `[triggers.gh_review_requested]`."""

    poll_interval_seconds: int = 300


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
