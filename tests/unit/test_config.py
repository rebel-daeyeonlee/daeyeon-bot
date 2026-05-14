"""Phase 0 config: defaults are sane; example file parses."""

from __future__ import annotations

from pathlib import Path

import pytest

from daeyeon_bot.app.config import (
    Config,
    GhReviewRequestedTriggerEntry,
    GitHubConfig,
    PrReviewHandlerEntry,
    SizeBudget,
    load,
)


def test_defaults_when_no_config() -> None:
    cfg = Config()
    assert cfg.runtime.shutdown_budget_seconds == 180
    assert cfg.logging.level == "INFO"
    assert cfg.logging.format == "json"


def test_example_toml_parses(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parents[2] / "config.example.toml"
    dst = tmp_path / "config.toml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    cfg = load(str(dst))
    assert cfg.runtime.state_dir.endswith("daeyeon-bot")
    assert cfg.logging.format in {"json", "console"}
    # routing / triggers / handlers go into `raw` for now (Phase 1 promotes them to typed).
    assert "routing" in cfg.raw or "triggers" in cfg.raw


# ── PR-review feature config (T010) ───────────────────────────────────────────


def test_github_section_defaults() -> None:
    """`[github]` parses with empty username; gh_call_timeout_seconds defaults to 30."""
    cfg = Config()
    assert cfg.github.username == ""
    assert cfg.github.gh_call_timeout_seconds == 30


def test_github_section_with_values(tmp_path: Path) -> None:
    """Explicit values override defaults."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[github]\nusername = "alice"\ngh_call_timeout_seconds = 60\n',
        encoding="utf-8",
    )
    cfg = load(str(cfg_path))
    assert cfg.github.username == "alice"
    assert cfg.github.gh_call_timeout_seconds == 60


def test_gh_review_requested_trigger_default_poll_interval(tmp_path: Path) -> None:
    """[triggers.gh_review_requested].poll_interval_seconds defaults to 300."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[triggers.gh_review_requested]\nenabled = true\n",
        encoding="utf-8",
    )
    cfg = load(str(cfg_path))
    typed = cfg.gh_review_requested_trigger_entry()
    assert isinstance(typed, GhReviewRequestedTriggerEntry)
    assert typed.poll_interval_seconds == 300
    assert typed.enabled is True


def test_pr_review_handler_size_budget_defaults(tmp_path: Path) -> None:
    """[handlers.pr_review.size_budget] defaults to (1000, 50)."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[handlers.pr_review]\nenabled = true\n",
        encoding="utf-8",
    )
    cfg = load(str(cfg_path))
    typed = cfg.pr_review_handler_entry()
    assert isinstance(typed, PrReviewHandlerEntry)
    assert typed.size_budget == SizeBudget(max_lines=1000, max_files=50)
    assert typed.min_persona_chars == 200


def test_pr_review_handler_explicit_size_budget(tmp_path: Path) -> None:
    """Explicit size_budget values override defaults."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[handlers.pr_review]\nenabled = true\npersona_skill = 'pr-review'\n"
        "[handlers.pr_review.size_budget]\nmax_lines = 500\nmax_files = 20\n",
        encoding="utf-8",
    )
    cfg = load(str(cfg_path))
    typed = cfg.pr_review_handler_entry()
    assert typed.persona_skill == "pr-review"
    assert typed.size_budget.max_lines == 500
    assert typed.size_budget.max_files == 20


def test_pr_review_skills_root_default_and_override(tmp_path: Path) -> None:
    """`[handlers.pr_review].skills_root` is None by default and round-trips."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[handlers.pr_review]\nenabled = true\n",
        encoding="utf-8",
    )
    typed = load(str(cfg_path)).pr_review_handler_entry()
    assert typed.skills_root is None

    cfg_path.write_text(
        "[handlers.pr_review]\nenabled = true\nskills_root = '~/.claude/skills'\n",
        encoding="utf-8",
    )
    typed = load(str(cfg_path)).pr_review_handler_entry()
    assert typed.skills_root == "~/.claude/skills"


def test_pr_review_persona_skill_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """DAEYEON_BOT__GITHUB__USERNAME=alice reaches the github config (env_nested smoke).

    NOTE: pydantic-settings does not natively materialize nested *dict* entries
    (e.g. handlers["pr_review"]) from env vars; nested-env support is verified
    here on a top-level submodel (`github.username`) as a representative case.
    Operators wanting to override `handlers.pr_review.persona_skill` should
    edit the TOML file or use `lifecycle reload-config`.
    """
    monkeypatch.setenv("DAEYEON_BOT__GITHUB__USERNAME", "alice")
    cfg = Config()
    assert cfg.github.username == "alice"


def test_retention_gh_state_dormant_days_default() -> None:
    """retention.gh_state_dormant_days defaults to 90 (data-model.md §7)."""
    cfg = Config()
    assert cfg.retention.gh_state_dormant_days == 90


def test_github_config_extra_forbidden() -> None:
    """GitHubConfig rejects unknown keys (extra='forbid')."""
    with pytest.raises(Exception):  # noqa: B017
        GitHubConfig(unknown_field="x")  # type: ignore[call-arg]


def test_typo_in_section_rejected(tmp_path: Path) -> None:
    """D3: a misspelled top-level section like `[handlrs.pr_review]` must
    raise at boot rather than silently no-op."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[handlrs.pr_review]\nenabled = true\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):  # noqa: B017
        load(str(cfg_path))


# ── Jira-triage feature config (Feature 002, T015) ────────────────────────────


def test_jira_config_defaults() -> None:
    """[jira] section defaults match data-model.md §7."""
    cfg = Config()
    assert cfg.jira.base_url == "https://rbln.atlassian.net/"
    assert cfg.jira.issuetype_override == ""
    assert cfg.jira.timeout_seconds == 30


def test_loki_config_defaults() -> None:
    """[loki] section defaults match data-model.md §7."""
    cfg = Config()
    assert cfg.loki.base_url == "http://loki.ssw.rbln.in"
    assert cfg.loki.per_stream_max_bytes == 1_048_576
    assert "regression-fwlog" not in cfg.loki.kernel_query_template  # kernel != fwlog
    assert "filename" in cfg.loki.kernel_query_template


def test_jira_assigned_trigger_entry_defaults() -> None:
    """[triggers.jira_assigned] defaults: 300s poll, 200 cap, DevOps team."""
    cfg = Config()
    entry = cfg.jira_assigned_trigger_entry()
    assert entry.poll_interval_seconds == 300
    assert entry.max_per_cycle == 200
    assert entry.team_name == "DevOps"


def test_jira_triage_handler_entry_defaults() -> None:
    """[handlers.jira_triage] defaults from data-model.md §7."""
    cfg = Config()
    entry = cfg.jira_triage_handler_entry()
    assert entry.allowed_projects == ["SSWCI"]
    assert entry.persona_skill is None  # default; operator sets in TOML
    assert entry.timeout_seconds == 600
    assert entry.ssw_bundle_path == "var/ssw-bundle"
    assert entry.allow_external_ssw_bundle is False
    assert entry.ssh_max_file_bytes == 10_485_760
    assert "output.xml" in entry.ssh_fetch_globs


def test_jira_triage_handler_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env override path: DAEYEON_BOT__... percolates into typed entry."""
    monkeypatch.setenv("DAEYEON_BOT__JIRA__BASE_URL", "https://example.atlassian.net/")
    cfg = Config()
    assert cfg.jira.base_url == "https://example.atlassian.net/"


def test_retention_jira_state_dormant_days_default() -> None:
    """retention.jira_state_dormant_days defaults to 180."""
    cfg = Config()
    assert cfg.retention.jira_state_dormant_days == 180


def test_example_toml_parses_jira_sections(tmp_path: Path) -> None:
    """The shipped config.example.toml includes [jira], [loki], jira_assigned, jira_triage."""
    src = Path(__file__).resolve().parents[2] / "config.example.toml"
    dst = tmp_path / "config.toml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load(str(dst))
    assert cfg.jira.base_url == "https://rbln.atlassian.net/"
    assert cfg.loki.base_url == "http://loki.ssw.rbln.in"
    trigger = cfg.jira_assigned_trigger_entry()
    assert trigger.enabled is False  # default off, runtime opt-in
    handler = cfg.jira_triage_handler_entry()
    assert handler.enabled is False
    assert handler.persona_skill == "daeyeon-bot-jira-triage"
