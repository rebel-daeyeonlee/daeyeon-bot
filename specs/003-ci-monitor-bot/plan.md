# Implementation Plan: CI Monitor / OnCall Triage Bot

**Branch**: `003-ci-monitor-bot` | **Date**: 2026-06-19 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/003-ci-monitor-bot/spec.md`

## Summary

When a CI-failure alert lands in one of the two Slack channels SSW DevOps
on-call actually lives in — `#ssw-devops-alerts` (`C09SEN8MH5M`) and
`#ssw-devops-help` (`C0A406KREHF`) — the bot polls, detects the new message,
parses it for a `repo + run_id` (and PR#/head SHA/failed jobs/consecutive-fail
count when present), pulls the failed run's logs read-only via
`gh run view <run_id> --repo <repo> --log-failed`, ANSI-strips → redacts →
error-anchored-truncates them, refreshes a project-local clone of the OnCall LLM
Wiki (`rebellions-sw/ssw-devops-oncall`) with `git pull --ff-only` and does a
signature-first ripgrep over `incidents/` (+ always-included
`recovery-playbook.md`), then calls Claude with the operator's CI-triage persona.
The persona returns a structured `TriageOutput` with a top-level
**attribution** judgement (`infra_env | product_regression | flaky | unknown`)
plus `classification`, `owner_area` (wiki domain enum), `confidence`, log
evidence, wiki matches, recommended action, and rerun advice (Korean prose +
English technical terms). The bot posts the result as a **single Slack
comment** — initially one-way to a `dry_run` test channel, then promoted to a
reply in the original alert thread.

The implementation extends the existing daemon by adding **one trigger**
(`slack_ci_alert`, polling) and **one handler** (`ci_triage`) plus two new
`infra/` adapters: `infra/slack.py` (Slack Web API over `httpx` — cursor
`conversations.history`, `conversations.replies`, `chat.postMessage`) and
`infra/oncall_wiki.py` (clone + `pull --ff-only` + two-layer path guard +
signature-first ripgrep). It **reuses** the existing `gh_cli` adapter (extended
with one read-only `run view --log-failed` method), the feature-002 Loki adapter
(`infra/loki.py`) for device-level alerts, the persona loader, the redaction
processor, and the outbox/dispatcher machinery wholesale. A new SQLite
migration `006_slack_ci_alert_state.sql` adds `slack_ci_alert_state` (per-channel
read cursor + cold-start seed flag — the structural analogue of
`gh_review_requested_state` / `jira_assigned_state`) and `ci_triage_audit`
(one row per posted/skipped/failed triage).

Auth uses **one new secrets key** (`slack_bot_token`, an `xoxb-` token for the
existing `dev_syssw_test` bot) threaded through the existing Keychain/0600/env
provider chain — no new provider class. The existing `UNIQUE(source,
source_dedup_key)` on `events` carries the deterministic dedup token
`sha256("slack-ci-alert|{channel_id}|{message_ts}").hexdigest()`, with a
secondary `repo+run_id` guard at the handler so the same run is triaged once.

## Technical Context

**Language/Version**: Python 3.12 (`requires-python = ">=3.12,<3.13"` in `pyproject.toml`).
**Primary Dependencies**: existing — `claude-agent-sdk`, `pydantic` (v2),
`pydantic-settings`, `structlog`, `aiosqlite`, `typer`, `keyring`,
`uuid-utils`, plus **`httpx`** (already a runtime dep since feature 002 — Slack
Web API reuses the same async-client convention as `infra/jira_client.py` /
`infra/loki.py`). **No new runtime deps.** GitHub access stays on the operator's
local `gh` CLI via subprocess (`infra/gh_cli.py`). `git` and `ripgrep` (`rg`)
must be on `PATH` (already true on every SSW dev machine; `ssw_bundle.py`
already shells out to `git`, and the wiki search uses `rg` like the
oncall-collect tooling does). Slack **MCP stays off the hot path** (it is
interactive-only / cannot run headless under cron) — the daemon calls the Slack
Web API directly with its own bot token. No `slack_sdk` library — `httpx` covers
`conversations.history` / `conversations.replies` / `chat.postMessage` directly.
The `oncall_wiki` git ops run under a **controlled, headless-safe subprocess
env** (`GIT_TERMINAL_PROMPT=0` + a `GIT_SSH_COMMAND` with `BatchMode=yes`,
`StrictHostKeyChecking=accept-new`, and a managed 0600 `UserKnownHostsFile`) —
see §`infra/oncall_wiki.py` for why this is *stronger* than the `ssw_bundle.py`
git precedent and how it mirrors the feature-002 `ssh_logs.py` known_hosts
discipline. This closes the headless-daemon first-clone-hang footgun
(launchd/systemd have no TTY/agent).
**Storage**: SQLite WAL (existing `state.db`). One additive migration:
`006_slack_ci_alert_state.sql` (001–005 already shipped; `005` is the jira
triage state).
**Testing**: pytest (`pytest-asyncio` mode=auto), pytest-cov; integration tests
use real `aiosqlite` against `tmp_path` DBs, a real git fixture for the wiki
clone/pull/ripgrep path, and fakes for Slack/gh/Claude.
**Target Platform**: macOS (launchd) + Linux (systemd) — same artifact, same
code paths.
**Project Type**: single-process daemon (existing `src/daeyeon_bot/`), not split.
**Performance Goals**: P1 manual triage posts within ~5 min (SC, Acceptance 1);
P2 auto triage within ~10 min of the alert (Acceptance 1). Polling cadence
default 120 s ⇒ p50 detection ~60 s, p95 ~120 s. Per-event handler wall-clock
budgeted at 600 s (mirrors `jira_triage`); dispatcher polls outbox every ~200 ms.
**Constraints**: one `--log-failed` payload is large (438 KB / 3000+ lines
measured) — never sent whole to Claude; error-anchored windows only. Slack Web
API tier-3 methods (`conversations.history`) allow ~50 req/min — two channels
every 120 s is trivially under budget. `chat.postMessage` is tier-3 as well.
The wiki clone is a small Obsidian vault (low MB); `pull --ff-only` is cheap.
Boot adds one Slack `auth.test` probe (~200 ms) **inside `app/container.py:build`,
gated on `ci_triage`/`slack_ci_alert` being enabled** (see Constitution Check —
this is NOT a change to the fixed boot order in `app/lifecycle.py`).
**Scale/Scope**: one operator. A handful of CI-failure alerts per day in steady
state, spiking during release-branch instability. Concurrency=1 means events
queue; the per-event wall-clock cap keeps one slow triage from blocking the
queue. A `max_per_cycle` cap on the trigger entry (see §Configuration and the
state machine) bounds the per-poll emit fan-out so a multi-hour outage does not
flood the queue on restart.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The repo has no `.specify/memory/constitution.md`. The de-facto constitution is
the set of stable contracts in `CLAUDE.md`, `CONTRACTS.md`, and `docs/PLAN.md`.
This plan is gated against those:

| Gate | Source | Status | Notes |
|---|---|---|---|
| Single-tenant, single-process | CLAUDE.md "What this is" | PASS | No multi-tenancy, no broker, no API-key auth, no webhook server introduced. Slack input is **polling**, not an inbound HTTP listener. |
| Module layering one-way (`core ← infra ← triggers/handlers ← app ← cli`) | CLAUDE.md §Module layering | PASS | New trigger / handler stay in their layers; Slack + wiki ops via `infra/`; pure types in `core/ci_triage/`. |
| One event = one transaction (no read-modify-write) | CLAUDE.md §One event, one cycle | PASS | Trigger UPSERTs `slack_ci_alert_state` cursor + writes `events`+`outbox` in one tx per emitted alert. |
| Boot order fixed | CLAUDE.md §Boot order | PASS | The fixed boot order in `app/lifecycle.py` is **not touched**. Verified: `lifecycle.py` step 6 (`_maybe_load_oauth_token`) loads **only** the Claude OAuth token — it is not a list-of-keys probe. Feature-specific secrets + their auth probe live in `app/container.py:build` (this is where feature 002 loads `jira_user`/`jira_api_token`/`ssw_automation_password` and runs `jira_client.myself()`). Accordingly **`slack_bot_token` is `load_secret()`-ed in `container.build` and the Slack `auth.test` probe runs there, both gated on `ci_triage`/`slack_ci_alert` being enabled**; `AuthError` → exit 78. **`container.build` also (a) validates that `dry_run_channel` and the resolved `post_target` channel are members of the post allowlist — a fat-fingered channel id raises `ConfigError` → exit 78 at boot, not one DLQ'd event at a time; and (b) runs a boot-time `oncall_wiki` git-reachability probe (a `git ls-remote` under the headless-safe env, gated on enabled) so a missing deploy key / unreachable remote fails loud at boot rather than hanging the first triage.** The polling trigger registers via the existing `app/registry.py:instantiate_trigger`. No boot-step reorder. |
| At-least-once + idempotent handler | CONTRACTS.md §1 | PASS | `ci_triage` is `idempotent=True`; `side_effect_key=None` (Slack `chat.postMessage` has no idempotency key to set). Cross-event dedup happens via `ci_triage_audit` lookup on `(channel_id, message_ts)` then `(repo, run_id)` **before** posting. The within-event post-then-audit window is the same residual as feature 002 — see §"At-least-once residual" below. |
| Outbox claim-row pattern | CONTRACTS.md §1 | PASS | Trigger uses `infra/outbox.py:insert_event` + `enqueue_handler`; handler returns `Ack`/`Retry`/`DeadLetter` only. `claim_one()` is untouched. |
| HandlerResult is the only exit | CONTRACTS.md §2 | PASS | Slack/gh `401`/`invalid_auth`/`token_revoked` → `AuthError` → daemon halt (exit 78); Slack `429`/`ratelimited` with `Retry-After` → `RateLimitError` → `Retry`; 5xx/timeout/transient git / wall-clock budget timeout → `TransientError` → `Retry` (dispatcher promotes to `DeadLetter` only at `MAX_TRANSIENT_ATTEMPTS=10`, `dispatcher.py:48,272` — the handler never counts attempts); missing persona / malformed Claude output after the **in-call** re-prompt loop → `DeadLetter` with explicit audit status. The mapping is **centralized in `dispatcher._run_one`**, so `ci_triage` inherits the existing `QuotaError` branch (`dispatcher.py:234`; the Claude SDK session is the only path that could raise it) and the `PermanentError → DeadLetter` / `AuthError → halt` rules **for free** — the handler raises typed `core.errors` or returns `Ack`/`Retry`/`DeadLetter` and never translates exceptions itself. Note `ConfigError` subclasses `PermanentError` (`errors.py:29`), so the channel-allowlist guard fails loud as **exit 78 only at boot** (in `container.build`, before the dispatcher exists); at handler time the same guard would `DeadLetter` (no halt, no post) — safe, and effectively unreachable since the allowlist is assembled from static config at boot. |
| Migration linear/additive | CLAUDE.md §Add a SQL column | PASS | One new file `006_slack_ci_alert_state.sql`. No edits to `001`–`005`. |
| Secrets discipline | CLAUDE.md §Secrets discipline | PASS | One new key (`slack_bot_token`; env form `SLACK_BOT_TOKEN`) threads through the same provider chain. `infra/logging.py` already scrubs `xox[baprs]-` Slack tokens at `logging.py:29` — no new pattern needed (asserted in a regression test before ship). **Defense-in-depth**: `container.build` also calls `register_literal_secret(slack_token)` (the same belt-and-suspenders `ssh_logs.py` uses for its weak-shape secret), so a rotated/non-canonical token form (config-app token, or a value pasted without the `xoxb-` prefix) is scrubbed by literal match even if it does not match the `xox[baprs]-` regex. The redaction regression test asserts both the canonical-shape scrub and a non-canonical-form literal scrub. |
| Registry: explicit `if name == ...` | CLAUDE.md §Add new handler/trigger | PASS | `ci_triage` and `slack_ci_alert` get explicit branches in `app/registry.py`. |
| No new runtime deps unless justified | CLAUDE.md feature-001/002 precedent | PASS | `httpx` already present (feature 002). No `slack_sdk`. GitHub via existing `gh` CLI. `rg`/`git` are PATH tools, not Python deps. |
| Read-only principle enforced in code | spec FR-001/FR-007/FR-010/Q(read-only) | PASS | `gh_cli` gains only `run view --log-failed` (read). `oncall_wiki.py` exposes only `ensure_fresh()` (clone + `pull --ff-only`) + `search()` + `ls_remote()` (boot probe) — no commit/push/reset method exists — replicates `ssw_bundle.py`'s **two-layer path guard**, and runs every git op under a headless-safe, prompt-disabled env (see §`infra/oncall_wiki.py`). Slack adapter's only write is `post_message`, and that write is **channel-allowlist-guarded** (see §`infra/slack.py`). |

**Violations**: none. **Complexity Tracking** below is empty.

## Project Structure

### Documentation (this feature)

```text
specs/003-ci-monitor-bot/
├── plan.md                          # this file
├── spec.md                          # /speckit.specify (with R1/R3 live-validated clarifications)
├── research.md                      # Phase 0
├── data-model.md                    # Phase 1 (incl. ci_triage_audit.event_id FK ON DELETE CASCADE + events-delete-cascades test note + NULL (repo,run_id) never-collapse note + concurrency=1 dependence of the secondary guard + raw_blob-is-non-secret-bearing invariant + the redaction-asymmetry cross-reference: INFO-logged log-failed windows ARE scrubbed, raw_blob/payload_json is NOT)
├── quickstart.md                    # Phase 1
├── contracts/                       # Phase 1
│   ├── slack-web-api-surface.md     # exact methods (conversations.history/replies, chat.postMessage — NO chat.update/chat.delete; force-supersede posts a NEW message + leaves the prior) + JSON shapes + cursor pagination + channel-allowlist post guard + boot token-absent (load_secret AuthError caught→rewrapped ConfigError) vs token-revoked (auth.test AuthError propagated) distinction + PAUSE skip-read/no-cursor-move note (edited in P2 to match reconciled Acceptance 2.6)
│   ├── alert-parse-surface.md       # the 3 alert sources + merged text/attachments[]/blocks regex rules + filter
│   ├── gh-run-log-surface.md        # `gh run view --log-failed` invocation + ANSI strip + error-anchor windows + dedicated non-`api` classifier (RunLogUnavailableError → skip, not retry)
│   ├── oncall-wiki-surface.md       # clone/pull --ff-only + headless-safe git env (GIT_TERMINAL_PROMPT=0 / BatchMode / 0600 known_hosts perm-check reused from ssh_logs.py:76-86 + accept-new pinning that ssh_logs does NOT do — achievable because OpenSSH not asyncssh) + known_hosts-before-ls_remote ordering + boot ls-remote probe + TWO-layer path guard + cold-start + partial/corrupt-clone detection + PINNED exact vault-relative paths (INCIDENTS_GLOB + RECOVERY_PLAYBOOK_PATH sentinel) + signature-first ripgrep scope + PINNED domain vocabulary (drift-guard source + re-pin trigger)
│   ├── loki-query-surface.md        # device-level dual-evidence path (LokiClient built in ci_triage container branch, works with feature 002 OFF; [loki] required when ci_triage enabled)
│   ├── claude-triage-output.md      # TriageOutput Pydantic schema + attribution/owner_area enums + anchor-strength heuristic + system prompt
│   └── persona-skill-format.md      # SKILL.md frontmatter+body contract (refs 001/002)
├── checklists/
│   └── requirements.md              # quality checklist
└── tasks.md                         # /speckit.tasks output (carries the implementer notes: per-candidate (NOT per-cycle) trigger commit boundary; register_literal_secret fires in container.build + clear_literal_secrets_for_testing between redaction-test cases; concurrency=1 inline manifest comment; FK ON DELETE CASCADE + cascade-on-events-delete test; NO handler-level attempt counting — timeout/Claude-SDK raise TransientError and the dispatcher promotes at MAX_TRANSIENT_ATTEMPTS=10, wiki-initial-clone-failure goes straight to wiki-less with no retry, malformed-Claude is an in-call 2-attempt loop; LokiClient built in the ci_triage container branch gated on ci_triage enabled (works with feature 002 OFF) + [loki] required when ci_triage enabled; token-absent load_secret AuthError caught→ConfigError vs auth.test AuthError propagated; known_hosts 0600 file established before ls_remote probe; empty-channel cold-start seeds no row)
```

### Source Code (repository root)

Existing layout (unchanged dirs abbreviated). New files marked **NEW**.

```text
src/daeyeon_bot/
├── core/                                    # pure domain — stdlib only
│   ├── events.py                            # (existing)
│   ├── manifest.py                          # (existing)
│   ├── protocols.py                         # (existing)
│   ├── results.py                           # (existing)
│   └── ci_triage/                           # NEW: domain types for this feature
│       ├── __init__.py                      # NEW
│       └── types.py                         # NEW: AlertRef, ParsedAlert, RunRef, FailedLog, WikiMatch, TriageOutput, AuditRow dataclasses + enums
├── infra/                                   # adapters — depend on core
│   ├── outbox.py                            # (existing)
│   ├── storage.py                           # (existing)
│   ├── claude.py                            # (existing)
│   ├── secrets.py                           # (existing — load_secret("slack_bot_token") reuses the chain; no code change unless a key-name helper is added)
│   ├── logging.py                           # (existing — already scrubs xox[baprs]- at line 29; add a regression test, not a new pattern)
│   ├── gh_cli.py                            # MODIFIED: add `run_view_log_failed(repo, run_id) -> str` (read-only)
│   ├── loki.py                              # (existing, feature 002 — REUSED for device-level dual-evidence path)
│   ├── persona_loader.py                    # (existing — REUSED, mtime hot-reload)
│   ├── slack.py                             # NEW: httpx wrapper — conversations.history (cursor), conversations.replies, chat.postMessage (thread + customize) with channel-allowlist post guard
│   ├── alert_parse.py                       # NEW: merge text+attachments[]+blocks → regex actions/runs/<id> + Grafana/Loki links + author/host extract
│   ├── oncall_wiki.py                       # NEW: var/ssw-devops-oncall/ clone manager (git pull --ff-only + two-layer path guard + signature-first ripgrep)
│   ├── slack_ci_alert_state.py             # NEW: slack_ci_alert_state CRUD (per-channel cursor; mirror of jira_triage_state.py)
│   ├── ci_triage_audit.py                   # NEW: ci_triage_audit CRUD
│   └── db/migrations/
│       ├── 001_init.sql                     # (existing) — DO NOT EDIT
│       ├── 002_gh_review_requested_state.sql  # (existing)
│       ├── 003_ratelimit_seed.sql           # (existing)
│       ├── 004_pr_review_audit_disallowed_repo.sql  # (existing)
│       ├── 005_jira_triage_state.sql        # (existing)
│       └── 006_slack_ci_alert_state.sql     # NEW
├── triggers/
│   ├── manual.py                            # (existing)
│   ├── gh_review_requested.py               # (existing)
│   ├── jira_assigned.py                     # (existing — structural template; max_per_cycle precedent at lines 95/239-245)
│   └── slack_ci_alert.py                    # NEW: polling loop over 2 channels, per-channel cursor + cold-start seed + max_per_cycle cap + staleness re-seed
├── handlers/
│   ├── echo.py                              # (existing)
│   ├── pr_review*.py                        # (existing)
│   ├── jira_triage*.py                      # (existing)
│   ├── ci_triage.py                         # NEW: parse → extract run → gh --log-failed → strip/redact/anchor → wiki search → Claude → post
│   ├── ci_triage_parsing.py                 # NEW: ANSI strip + error-anchored truncation helpers (pure)
│   └── ci_triage_schemas.py                 # NEW: TriageOutput Pydantic v2 model + attribution/owner_area enums
├── app/
│   ├── lifecycle.py                         # (existing — NOT TOUCHED; boot order is fixed, no Slack secret loaded here)
│   ├── registry.py                          # MODIFIED: add `ci_triage` (CiTriageDeps) + `slack_ci_alert` (SlackCiAlertDeps) branches
│   ├── config.py                            # MODIFIED: add SlackConfig, OncallWikiConfig, SlackCiAlertTriggerEntry (incl. max_per_cycle, staleness_seconds), CiTriageHandlerEntry + accessors
│   ├── container.py                         # MODIFIED: load_secret("slack_bot_token") (catch load-time AuthError→ConfigError for doctor clarity) + Slack auth.test probe (gated on enabled) + instantiate SlackClient, OncallWiki, + LokiClient in the ci_triage branch (gated on ci_triage enabled, shared with jira if both on) + ls_remote boot probe + channel-allowlist boot validation; build CiTriageDeps/SlackCiAlertDeps
│   ├── supervisor.py                        # (existing TriggerSupervisor — generic quarantine/failure-window tracker; the slack_ci_alert poller is launched via the supervised run loop fed by the trigger registry, exactly like jira_assigned. No new file role.)
│   └── prune.py                             # (existing — ci_triage_audit cascades with its events row; slack_ci_alert_state is 2 rows, never pruned; change is minimal/none)
└── cli/
    ├── dev.py                               # MODIFIED: add `dev fire-ci-triage --repo <r> --run <id> [--channel <test>] [--force]` (dedicated subcommand, mirrors `fire-pr-review`/`fire-jira-triage`)
    ├── inspect.py                           # MODIFIED: add `inspect ci-triage` (no arg = per-channel cursor state + recent-audit summary; `--message-ts <ts>` = single audit row). `ops doctor` also gains a slack_ci_alert cursor-age + quarantine liveness line.
    └── ...                                  # (other existing files: untouched)

config.example.toml                          # MODIFIED: add [slack], [oncall_wiki], [triggers.slack_ci_alert], [handlers.ci_triage], routing entries
.gitignore                                   # already has var/ (feature 002, verified) — covers var/ssw-devops-oncall/; no change

.claude/skills/daeyeon-bot-ci-triage/SKILL.md  # NEW: bundled default persona

var/                                         # (existing gitignored dir from feature 002)
└── ssw-devops-oncall/                       # auto-managed by infra/oncall_wiki.py

tests/
├── unit/
│   ├── test_slack.py                        # NEW (httpx mock transport — cursor pagination, thread reply, customize, channel-allowlist post guard)
│   ├── test_alert_parse.py                  # NEW (the 3 real alert shapes: sukju-bot / dev_syssw_test / SSW-Alert-Bot attachments)
│   ├── test_oncall_wiki.py                  # NEW (fixture vault under tmp_path: two-layer path guard, pull --ff-only, initial-clone-failure rule, signature ripgrep ranking)
│   ├── test_ci_triage_parsing.py            # NEW (ANSI strip + error-anchored truncation + strip→redact ordering on the 438KB fixture incl. GITHUB_TOKEN + AWS key)
│   ├── test_ci_triage_schemas.py            # NEW (TriageOutput validation: attribution/owner_area enums, anchor-strength → low-confidence path)
│   ├── test_slack_ci_alert_state.py         # NEW (per-channel cursor advance + cold-start seed)
│   ├── test_ci_triage_audit.py              # NEW
│   ├── test_ci_triage_handler.py            # NEW (all fakes + FakeClaudeSession; dry_run vs thread reply, skip cases)
│   ├── test_slack_ci_alert_trigger.py       # NEW (FakeClock + FakeSlack; cold-start, filter, emit-on-new, max_per_cycle cap, staleness re-seed)
│   ├── test_gh_cli_log_failed.py            # NEW (FakeGh / subprocess stub for run view --log-failed)
│   └── test_migration_006.py                # NEW
├── integration/
│   └── test_ci_triage_e2e.py                # NEW (real aiosqlite + real git wiki fixture + fakes for Slack/gh/Claude)
└── fakes/
    ├── slack.py                             # NEW (FakeSlack: scripted history pages + capture posted messages)
    └── oncall_wiki.py                       # NEW (or shared git-fixture helper)
```

**Structure Decision**: extend the existing single-project layout. The feature
lands as **one new trigger + one new handler + one additive migration + two new
config sections + two new `infra/` adapters** (`slack.py`, `oncall_wiki.py`)
plus small helpers (`alert_parse.py`, `slack_ci_alert_state.py`,
`ci_triage_audit.py`, `ci_triage_parsing.py`), an extension to the existing
`gh_cli.py`, reuse of the existing `loki.py` and `persona_loader.py`, a bundled
persona SKILL.md, and the existing gitignored `var/`. This matches the
`CLAUDE.md` "Add a new handler / Add a new trigger / Add a SQL column" recipes,
exactly mirroring how feature 002 extended them. `app/lifecycle.py` is **not**
touched — feature secrets and the Slack probe land in `app/container.py`, where
feature 002 put its Jira probe.

**Implementation scope estimate**: 1 trigger + 1 handler (+ 2 handler helper
modules) + 2 new `infra/` adapters + 2 small `infra/` CRUD modules + 1 `gh_cli`
method + 1 SQL migration + config edits + 1 CLI edit + 1 registry edit + 1
persona SKILL.md ≈ **~14 source files + ~1300 lines** + ~12 test files. Anything
materially larger should prompt a re-scope discussion.

## Reuse Map

| Concern | Reused asset | New work |
|---|---|---|
| at-least-once / dedup / recovery | `infra/outbox.py`, `app/dispatcher.py` | new event types + dedup token only |
| SQLite WAL + linear migrations | `infra/storage.py`, `infra/db/migrations/` | `006_*.sql` |
| secret redaction (incl. `xox*`, gh PAT, AWS, entropy) | `infra/logging.py` | regression test asserting Slack-token + gh-PAT + AWS-key scrub on a real `--log-failed` fixture |
| Claude SDK session (NOT `claude -p`) | `infra/claude.py` via `ctx.claude_session_factory()` | `ci_triage_schemas.py` output model |
| persona SKILL.md mtime hot-reload | `infra/persona_loader.py` (used by pr_review/jira_triage) | `daeyeon-bot-ci-triage` SKILL.md |
| typed config + env override | `app/config.py` idiom (`JiraConfig`, `*TriggerEntry`, `*HandlerEntry`, accessors) | `SlackConfig`, `OncallWikiConfig`, `SlackCiAlertTriggerEntry`, `CiTriageHandlerEntry` |
| supervisor quarantine / PAUSE guard | `app/supervisor.py` (`TriggerSupervisor`), PAUSE kill-switch, `pause_check` | wire `slack_ci_alert` poller into the supervised run loop + `PermanentFailureReporter` |
| feature secrets + boot probe location | `app/container.py:build` (Jira: `load_secret` + `jira_client.myself()`) | `load_secret("slack_bot_token")` + Slack `auth.test`, gated on enabled |
| `gh` CLI subprocess adapter | `infra/gh_cli.py` (`_run`, `_raise_error`, 5xx classifier) | add `run_view_log_failed()` **with its own small non-`api` classifier** — see §`gh_cli.py` extension (the existing `_raise_error` is `gh api`-shaped and would mis-map a not-found/expired run log to `TransientError` → infinite Retry) |
| managed known_hosts **0600 perm-check only** (NOT host-key policy) | `infra/ssh_logs.py:76-86` (create-0600-if-absent / refuse-if-loose) — note `ssh_logs._fetch` itself uses `known_hosts=None` (verification OFF), which `oncall_wiki` does NOT copy | `infra/oncall_wiki.py` reuses the 0600 perm-check, sets `GIT_TERMINAL_PROMPT=0` + `GIT_SSH_COMMAND` with a `<state_dir>/oncall_wiki_known_hosts` file (0600), and **adds `accept-new` host-key pinning `ssh_logs` does not do** |
| literal-secret registration (defense in depth) | `infra/logging.py:register_literal_secret` (used by `ssh_logs.py`) | `container.build` registers the loaded `slack_bot_token` literal |
| Loki adapter (device-level dual evidence) | `infra/loki.py` (feature 002) | **`container.build` constructs a `LokiClient` in the ci_triage deps branch when ci_triage is enabled, independent of feature 002** (the verified container builds `LokiClient` ONLY inside `_build_jira_deps`, `container.py:250-253`) — see §`infra/loki.py` reuse below |
| audit table pattern | `pr_review_audit`, `jira_triage_audit` | `ci_triage_audit` |
| per-channel state machine (5-case) | `gh_review_requested_state`, `jira_assigned_state` | `slack_ci_alert_state` cursor |
| `max_per_cycle` emit cap + warn log | `jira_assigned.py` (`max_per_cycle` field + `max_per_cycle_hit` warning) | `SlackCiAlertTriggerEntry.max_per_cycle` wired into CASE 2 |
| HTTP client convention | `httpx` async client in `infra/jira_client.py` / `infra/loki.py` | `infra/slack.py` |
| git clone + two-layer **path guard** | `infra/ssw_bundle.py` (`__post_init__` hard-ban + inside-root check) | `infra/oncall_wiki.py` replicates the **path guard** verbatim — but **deliberately does NOT copy `ssw_bundle._git_run`'s (unset) git env**: it adds the headless-safe env from the `ssh_logs.py` precedent (see next row + §`infra/oncall_wiki.py`) |
| **NEW** | — | `infra/slack.py`, `infra/oncall_wiki.py`, `infra/alert_parse.py`, `triggers/slack_ci_alert.py`, `handlers/ci_triage*.py` |

## New Components

### Trigger `slack_ci_alert` (`triggers/slack_ci_alert.py`)

Polling loop (default 120 s) over the two channel IDs. Constructed with the
`jira_assigned.py` dep shape — `storage_factory`, `clock`,
`poll_interval_seconds`, **`max_per_cycle`**, **`staleness_seconds`**,
`pause_check`, `permanent_failure_reporter`, plus `slack` + the channel-id
tuple + the candidate-filter author set — and exposes `MANIFEST`. The 120 s
cadence, `max_per_cycle`, and `staleness_seconds` are **constructor args fed from
`SlackCiAlertTriggerEntry`**, not `TriggerManifest` fields (verified:
`TriggerManifest` carries only `name`/`source`/`retryable_at_source` per
CONTRACTS.md §3, exactly as `jira_assigned` passes `poll_interval_seconds` /
`max_per_cycle` through its constructor).

Per channel it pages `conversations.history` forward from the stored cursor,
applies the CI-failure candidate filter (known bot author OR contains
`github.com/.../actions/runs/<id>`), and for each candidate inside a single
`aiosqlite` tx: advances the channel cursor and emits one event — **capped at
`max_per_cycle` candidates per channel per cycle** (mirrors
`jira_assigned.py:239-245`). When the cap truncates a page it logs
`slack_ci_alert.max_per_cycle_hit` and advances the cursor only as far as the
last emitted candidate, so the remainder is picked up next cycle (no message is
skipped). See the state machine for the exact CASE-2 / CASE-3 cursor-advance
rule and the large-gap (staleness) re-seed. Mirrors `jira_assigned.py`'s
`PermanentFailureReporter` / `pause_check` / `clock` shape. Cold-start: on the
very first poll per channel (no state row) it seeds `last_seen_ts = channel's
current latest ts`, `seeded = 1`, and emits nothing (no retroactive triage).

### Handler `ci_triage` (`handlers/ci_triage.py`)

`idempotent=True`, `concurrency=1`, `side_effect_key=None`,
`accepts=["slack.ci_alert", "ci.triage.manual"]`. **`concurrency=1` carries an
inline code comment in the manifest** noting that the secondary `(repo, run_id)`
duplicate guard is an audit *lookup*, not a SQL `UNIQUE` constraint, and is
correct **only** because `concurrency=1` serializes claims (see §"Secondary
`repo+run_id` guard") — so it is not bumped casually during a release-branch alert
storm without first converting the guard to a real constraint / claimed-row lock. The real protocol signature is
`async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult`
(`core/protocols.py:47`); the Claude session is reached via
`ctx.claude_session_factory()` exactly as `jira_triage.py:667` does — **not** the
`handle(event, *, claude)` shorthand the CLAUDE.md recipe prints. Self-enforces
the 600 s wall-clock with `asyncio.wait_for`. Pipeline detailed in §Pipeline
below. Constructed with `slack`, `gh`, `oncall_wiki`, `loki`, `persona_loader`,
`config`, `loki_config`, `db`, and optional `pause_guard` — mirroring
`JiraTriageHandler`'s dep shape and the `CiTriageDeps` dataclass in
`app/registry.py`. (Implementation tasks MUST use the `ctx`-based signature.)
**The `loki` dep comes from a `LokiClient` that `container.build` constructs in
the ci_triage deps branch (gated on ci_triage enabled, shared with the jira branch
when both are on), reading `config.loki` — so the device-level path works even when
feature 002 is disabled; see §`infra/loki.py` reuse.** `[loki]` config is required
when `[handlers.ci_triage].enabled = true` (validated at boot).

### `infra/slack.py`

`httpx.AsyncClient` wrapper, bearer `slack_bot_token`. Methods:
- `auth_test() -> SlackIdentity` — boot probe (called from `container.build`).
- `history(channel_id, oldest_ts, cursor) -> HistoryPage` — `conversations.history`, cursor pagination, returns messages + `next_cursor`.
- `replies(channel_id, thread_ts) -> list[Message]` — `conversations.replies` (thread context if needed).
- `post_message(channel_id, text, *, thread_ts=None, username, icon_emoji) -> PostResult` — `chat.postMessage`; uses `chat:write.customize` (`username="CI Triage"`, dedicated icon) and optional `thread_ts` for the thread reply. **The only write method.**

**Channel-allowlist post guard** (closes the `chat:write.public` footgun): the
constructor takes an explicit `allowed_post_channels` tuple — the two known
channel ids **plus** the configured `dry_run_channel`. The allowlist is assembled
in `container.build` from the two `[triggers.slack_ci_alert].channels` ids +
`[handlers.ci_triage].dry_run_channel`. **The guard is primarily a boot
invariant**: `container.build` validates that `dry_run_channel` and the resolved
`post_target` channel are both members of the allowlist and raises `ConfigError`
→ exit 78 **at boot** if not — so a fat-fingered channel id is caught before any
alert is processed, not one DLQ'd event at a time. `post_message` re-checks the
target at handler time as a belt-and-suspenders guard; because the allowlist is
static config assembled at boot, a handler-time violation is effectively
unreachable, and if it ever fired it would `DeadLetter` (no halt, no post — see
the HandlerResult Constitution row) rather than broadcasting to a wrong channel.
Either way `chat:write.public` cannot be abused by a misconfigured `post_target`.

Error mapping helper: `invalid_auth`/`token_revoked`/`account_inactive` →
`AuthError`; `ratelimited` (HTTP 429, `Retry-After`) → `RateLimitError`; 5xx /
timeout → `TransientError`; other `ok:false` → `PermanentError`. `not_in_channel`
on read is a `PermanentError` (operator must invite the bot — already done).

**Boot probe distinguishes token-absent from token-revoked (doctor clarity) —
disambiguated by which *call site* raised, NOT by a non-existent empty return.**
Verified against the real adapter contract: `load_secret` **never returns an
empty/None value** — a missing key raises `AuthError` directly
(`KeychainSecrets._lookup` "keychain: no secret …" `secrets.py:71`;
`FileSecrets._read_token` "file secrets: missing …" `secrets.py:101`;
`EnvSecrets` "… not set" `secrets.py:136`). There is no `if not token: return`
path, so an `if not token: raise ConfigError` guard would be dead code that can
never fire, and both missing-token and revoked-token would otherwise surface as
the *same* `AuthError` — defeating the doctor-clarity goal. The plan therefore
disambiguates by **where the `AuthError` came from**, in `container.build` when
the feature is enabled:
- **token absent / never configured** — `load_secret("slack_bot_token")` itself
  raises `AuthError` (before `auth.test` ever runs). `container.build` **catches
  that `load_secret`-time `AuthError` and re-raises it as**
  `ConfigError("slack_bot_token not configured; set it or disable
  [handlers.ci_triage]")`. `ConfigError` still maps to exit 78 (it subclasses
  `PermanentError`, `errors.py:29`), but the *message* now says "you never set the
  token". This is the expected state for an operator who turned the feature on
  before provisioning the token.
- **token present but rejected** — `load_secret` succeeds, then `auth.test`
  returns `invalid_auth` / `token_revoked` / `account_inactive`, which the Slack
  adapter maps to `AuthError("slack auth.test rejected the configured token")`.
  This `AuthError` is **left to propagate** (not re-wrapped) → exit 78.
Both exit 78, but the distinct messages let doctor/runbook tell "you forgot to set
the token" (caught-and-rewrapped `load_secret` `AuthError` → `ConfigError`) from
"your token was revoked — rotate it" (`auth.test`-time `AuthError`). The
disambiguation is purely "which call raised", with no reliance on a falsy return.
`test_container_boot_validation` asserts both messages: a missing-token boot
yields the `ConfigError` "not configured" message; a rejecting fake `auth.test`
yields the `AuthError` "rejected the configured token" message.

### `infra/alert_parse.py`

Pure functions. `merge_message_text(msg) -> str` concatenates
`text` + every `attachments[].{title,text,fallback}` + (future) `blocks[].text`
— **all three**, because `SSW-Alert-Bot` content lives only in
`attachments[].text`. `extract_run_ref(merged) -> RunRef | None` regexes
`github.com/<owner>/<repo>/actions/runs/<id>` → `(repo, run_id)`.
`extract_pr_meta(merged)` best-effort pulls PR# / head SHA / failed job /
consecutive-fail-N (sukju-bot structured fields). `extract_loki_window(merged)`
pulls host + time window from `dev_syssw_test`/`SSW-Alert-Bot` Grafana/Loki
links (feeds the device-level dual-evidence path). `is_ci_failure_candidate(msg)`
implements the filter (known bot author set
`{sukju-bot, dev_syssw_test (U069J27G2G6), SSW-Alert-Bot (U09RJGLLPLZ)}` OR
contains an `actions/runs` link).

### `infra/oncall_wiki.py`

Project-local clone at `var/ssw-devops-oncall/` (gitignored). `ensure_fresh()`
clones if absent then `git pull --ff-only` (read-only — **no commit/push/reset
method exists**, mirroring `ssw_bundle.py`).

**Headless-safe git env (BLOCKING fix — do NOT copy `ssw_bundle.py`'s git env
verbatim).** `ssw_bundle._git_run` sets *no* controlled subprocess env; that is
safe only because it operates on an already-cloned working tree on a dev machine.
`oncall_wiki` must do a **first-ever clone of a brand-new remote** the daemon has
never seen, under a headless launchd/systemd process with **no TTY and no
ssh-agent** (verified: the launchd plist injects only `PATH`+`HOME`,
`ProcessType=Background`; the systemd `--user` unit has no guaranteed agent). A
naive SSH clone would block on host-key verification / credential prompts until
the 600 s `asyncio.wait_for` budget, then `Retry`, then hang another 600 s —
wedging `concurrency=1` and stalling the queue. **`oncall_wiki._git_run`
therefore sets, on every git invocation, an explicit env.**

**Precedent boundary (do NOT copy `ssh_logs.py` host-key handling verbatim).**
`oncall_wiki` reuses *only* the **0600 managed-known_hosts perm-check** from
`ssh_logs.py` (`ssh_logs.py:76-86`: create the file 0600 if absent, refuse it if
group/other-readable). It deliberately does **not** copy `ssh_logs.py`'s actual
host-key policy: `ssh_logs._fetch` passes `known_hosts=None` to asyncssh
(`ssh_logs.py:~115-127`), which **disables host-key verification entirely** —
intentional for re-imaged, key-churning internal lab hosts, but the wrong choice
for a long-lived GitHub remote. `oncall_wiki` instead **adds `accept-new` host-key
pinning that `ssh_logs` does not do**, so an implementer copying `ssh_logs`
verbatim must not inherit `known_hosts=None` (which would defeat the pinning this
adapter wants). The resulting env, set on every git invocation, is:
- `GIT_TERMINAL_PROMPT=0` — fail fast instead of prompting for credentials.
- `GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=<state_dir>/oncall_wiki_known_hosts"`
  — `BatchMode=yes` guarantees a non-interactive failure (never a prompt);
  `accept-new` trusts the host key on first contact then pins it (stronger than
  `ssh_logs`'s `known_hosts=None`); the known_hosts file is created 0600 and
  perm-checked using the same discipline as `ssh_logs.py:76-86`.

**Why `accept-new` pinning is achievable here but was abandoned in `ssh_logs.py`
(state in `contracts/oncall-wiki-surface.md`).** `ssh_logs.py` uses **asyncssh**,
which could not back `accept-new` with a bare known_hosts path (the in-code comment
at `ssh_logs.py:~108-122` records why the `accept-new` design was dropped there and
why it fell back to `known_hosts=None`). `oncall_wiki` uses **git over OpenSSH**
(via `GIT_SSH_COMMAND`), where `StrictHostKeyChecking=accept-new` is a
first-class, supported OpenSSH option. So the stronger pinning the plan wants is
implementable precisely because this adapter is OpenSSH-backed, not asyncssh —
the divergence is intentional and the contract doc must say so, so no one
"reconciles" the two adapters back to `known_hosts=None`.

The config also supports an **HTTPS `remote_url`** (e.g.
`https://github.com/rebellions-sw/ssw-devops-oncall.git`) which reuses the
operator's existing `gh` credential helper and sidesteps SSH-key provisioning in
the deploy env entirely; SSH remains supported for operators who provision a
deploy key. Either way, the **boot-time `ls_remote()` reachability probe** (a
`git ls-remote --quiet <remote_url>` under the same headless-safe env, run in
`container.build` gated on enabled) makes a missing deploy key / unreachable
remote fail loud as `ConfigError`/`AuthError` → exit 78 **at boot**, never as a
600 s first-triage hang.

**Ordering invariant (BLOCKING-adjacent, pinned in tasks.md).** The
`ls_remote()` probe's own `ssh` uses the managed `<state_dir>/oncall_wiki_known_hosts`
file via `GIT_SSH_COMMAND` (`-o UserKnownHostsFile=…`). With `accept-new`, an
**absent** known_hosts file is created on first contact — but the file's parent
dir must exist and the 0600 create/perm-check must have run, or the probe itself
could fail. So `OncallWiki.__init__` (or `ensure_known_hosts()`) **creates and
perm-checks the 0600 known_hosts file as part of construction**, and
`container.build` constructs the `OncallWiki` adapter (which establishes the
known_hosts file) **before** calling `ls_remote()`. A boot test asserts this
ordering (known_hosts file exists 0600 before the probe runs). A unit test asserts
the git env carries `GIT_TERMINAL_PROMPT=0` + `BatchMode=yes` and that the
known_hosts path is 0600.

**Two-layer path guard (replicated verbatim from `ssw_bundle.py:__post_init__`,
lines 65-80).** `OncallWiki.__post_init__` enforces, in order:
1. **Unconditional hard-ban** — resolve `Path.home() / "ssw-devops-oncall"` and
   raise `ConfigError` if `clone_path` resolves to it, **regardless of
   `allow_external`**. This protects the operator's live working vault
   (spec FR-007 / Q-OnCall-Wiki: "운영자의 `~/ssw-devops-oncall`은 건드리지 않는다").
2. **Inside-project-root check** — `clone_path` must resolve inside
   `project_root` unless `allow_external=True`.
Default `clone_path` stays `var/ssw-devops-oncall/`. `test_oncall_wiki.py`
asserts **both** guards fire (mirroring the `ssw_bundle` guard tests).

**Cold-start / transient-failure rule (split by clone existence) — NO
handler-level retry counting.** The handler has no access to the outbox `attempt`
counter (`HandlerContext` exposes only `clock`/`trace_id`/`claude_session_factory`,
`core/protocols.py:26-31`) and CONTRACTS forbids it counting attempts, so an
earlier draft's "Retry **once** then fall through" is **not implementable** and is
removed. The two cases are each a single, attempt-free decision:
- Transient failure (network) **with an existing clone** → continue with the
  stale clone, record `wiki_error`; do **not** skip or retry. Triage proceeds
  with possibly-stale wiki context (the prompt rule already tolerates wiki being
  supporting-only).
- Transient failure **with no existing clone** (the very first triage after
  deploy, or after `var/` is wiped — a routine, rebuildable ops action) → fall
  **straight through** to **wiki-less triage with `confidence` capped at `low`**
  and `wiki_error` recorded. **No `Retry`** is raised for this case. This is the
  chosen resolution (vs. raising `TransientError` and letting the dispatcher retry
  up to `MAX_TRANSIENT_ATTEMPTS=10`): a missing wiki is a degraded-but-honest
  triage, not a crash, and a first-run network blip should yield a useful
  low-confidence first-pass *now* rather than stalling the `concurrency=1` queue
  slot through ten backoff cycles before degrading anyway. (Genuinely permanent
  remote misconfiguration is already caught loud at boot by the `ls_remote()`
  reachability probe — see below — so the handler-time path only ever sees
  transient blips, for which immediate degradation is the right call.)

**Partial/corrupt-clone detection (before the retry/fall-through).** A clone
interrupted mid-network can leave a directory with a `.git` (and the right
`origin` URL) but **no/empty worktree** — the `ssw_bundle`-style origin-URL check
would pass on it and `search()` would then silently return nothing. So
`ensure_fresh()` treats a clone as healthy only if BOTH `.git` exists AND the
sentinel tracked path (`RECOVERY_PLAYBOOK_PATH` pinned in
`contracts/oncall-wiki-surface.md` — the same path `search()` always needs) is
present and non-empty. A clone that fails the sentinel check is
**removed and re-cloned** (it is fully rebuildable — `var/ssw-devops-oncall/` is
gitignored and disposable) before the "initial clone fails → wiki-less (no retry)"
fall-through is considered. `test_oncall_wiki.py` covers both
"initial clone fails, no prior clone" **and** "partial/corrupt clone present
(`.git` + origin OK but empty worktree) → detected, removed, re-cloned".

`search(signatures, phrases) -> list[WikiMatch]` does signature-first ripgrep:
match log error signatures against incident frontmatter `signature:` first, then
body, weight multi-word error phrases over single noise tokens, scope to the
incidents directory, and **always** include the recovery playbook.

**Exact in-repo paths are pinned in `contracts/oncall-wiki-surface.md` (resolves
the path-inconsistency review finding).** Spec FR-007 writes the scope shorthand
as `incidents/` + `recovery-playbook.md`; earlier plan drafts wrote
`wiki/oncall/incidents/*.md` + `wiki/notes/recovery-playbook.md`. The intent is
unambiguous but the literal vault-relative paths must match the **live vault
layout** exactly, because the same paths are used by (a) the ripgrep scope and
(b) the corrupt-clone sentinel check (a tracked path that `search()` always needs).
`contracts/oncall-wiki-surface.md` therefore **pins the verified vault-relative
paths read from the live `ssw-devops-oncall` checkout** as the single source of
truth (`INCIDENTS_GLOB` and `RECOVERY_PLAYBOOK_PATH` constants), and the sentinel
check, `search()` scope, and the corrupt-clone detector all reference those same
pinned constants — never a hand-typed path. The plan and spec use the shorthand
prose; the contract holds the exact strings. (The sentinel path used by the
corrupt-clone detector below is `RECOVERY_PLAYBOOK_PATH`, the same constant
`search()` always includes.)

### `infra/loki.py` reuse (device-level path) — Loki client provenance when feature 002 is OFF

The device-level dual-evidence path (CP/SMC FW alerts with no useful GitHub run
log, e.g. `0x50555746 device unreachable`) reuses the feature-002 `infra/loki.py`
`LokiClient`. But that client is currently constructed **only** inside the Jira
deps branch (`container.py:250-253`, gated on `jira_triage`/`jira_assigned` being
enabled) and reads from `config.loki.*`. `ci_triage` lands behind its **own**
`[handlers.ci_triage].enabled` flag with **no dependency on feature 002**, and an
operator enabling ci_triage while leaving jira_triage disabled is an expected,
valid config under the opt-in landing model. So the Loki client must be obtained
independently. The chosen resolution (option a from the review, the stronger one
since the device path is in v1 scope per SC-004 / spec R1):

- **`container.build` constructs (or shares) a `LokiClient` in the ci_triage deps
  branch, gated on `ci_triage` being enabled**, reusing the **same `config.loki`
  section** as feature 002. If both ci_triage and jira_triage are enabled, the
  single already-built `LokiClient` is shared (no second client); if only
  ci_triage is enabled, the ci_triage branch builds it. `CiTriageDeps.loki` is
  populated from this client.
- **Config requirement made explicit**: `[loki]` config **must be present when
  `[handlers.ci_triage].enabled = true`** (same `base_url`/`timeout_seconds`/
  `per_stream_max_bytes` shape feature 002 uses). `container.build` validates this
  at boot — a ci_triage-enabled config with no `[loki]` section raises
  `ConfigError` → exit 78, consistent with the channel-allowlist and `ls_remote`
  boot validations (fail loud at boot, never a per-event crash on the first
  device-level alert).
- **Graceful absence at handler time (belt-and-suspenders)**: if for any reason no
  Loki window is extractable from the alert text (not a config problem — the alert
  simply carries no host+window), the device-level path is skipped and the handler
  degrades to `confidence: low` / `attribution: unknown` per the prompt rule
  rather than crashing (matches §Risks "Device-level evidence sufficiency").

**Boot test (required, added to P1):** `test_container_boot_validation` asserts
**ci_triage boots cleanly with `jira_triage` DISABLED** — the `LokiClient` is
present in `CiTriageDeps` from the ci_triage branch — and that a ci_triage-enabled
config missing `[loki]` raises `ConfigError` at boot.

### `gh_cli.py` extension

Add `async def run_view_log_failed(self, repo: str, run_id: str) -> str` →
`gh run view <run_id> --repo <repo> --log-failed`. Read-only — no rerun/dispatch
method added. It reuses the existing `_run` subprocess plumbing **but NOT the
`_raise_error` classifier wholesale** (BLOCKING-adjacent correctness fix):

`_raise_error` (verified `gh_cli.py:404-428`) is **`gh api`-shaped** — it parses
an `HTTP <code>` out of stderr and, finding none, falls through to
`TransientError` (line 428). `gh run view --log-failed` is *not* a `gh api` call:
a not-found / deleted / **logs-expired-by-GitHub-retention** / re-run-overwrote
run emits human-readable stderr with **no HTTP code**, so blanket reuse would map
a *permanently-unavailable* log to `Retry(DEFAULT_BACKOFF_S)` **forever** — a
silently stuck queue entry, exactly the toil this bot exists to remove. So
`run_view_log_failed` gets a **dedicated small classifier**:
- auth phrases (`_AUTH_PHRASES` already match for `gh run`) → `AuthError` (halt) — preserved.
- `ratelimited` / rate phrases → `RateLimitError` → `Retry`.
- a real HTTP 5xx in stderr → `TransientError` → `Retry`.
- **log/run unavailable** — stderr matching not-found / "no logs found" /
  "expired" / "could not find any workflow run" / deleted-run phrasing (no HTTP
  code) → a dedicated `RunLogUnavailableError(PermanentError)`. The handler maps
  this to **`Ack` + audit(`skipped_log_unavailable`)** (a new audit status), NOT
  `Retry` — the run log is gone and will not come back; re-queueing wastes the
  slot. (If the alert also carries a Loki window, the handler instead falls
  through to the device-level Loki path before recording the skip.)
- anything else genuinely transient (network/timeout) → `TransientError` → `Retry`.

`test_gh_cli_log_failed.py` asserts the not-found / logs-expired stderr maps to
`RunLogUnavailableError` (→ `Ack`/skip), not `TransientError` (→ infinite Retry),
and that auth stderr still maps to `AuthError`.

### Persona `daeyeon-bot-ci-triage`

Bundled default at `.claude/skills/daeyeon-bot-ci-triage/SKILL.md`, user-home
override supported, selected via `[handlers.ci_triage].persona_skill`, mtime-stat
per event → reload on change. Output language: Korean prose + English technical
terms / paths / raw log lines. Prompt rule enforced: **log = primary evidence,
wiki = supporting; do not assert a wiki link without a matching log anchor;
insufficient evidence ⇒ `attribution: unknown` / `confidence: low`.**

### Migration `006`, config models, registry/container wiring, CLI

Covered in §Data Model, §Configuration, and §Project Structure. CLI:
`dev fire-ci-triage --repo <r> --run <id> [--channel <test>] [--force]` (a
dedicated subcommand, mirroring the `fire-pr-review` / `fire-jira-triage`
precedent — `dev fire` is reserved for the `manual` trigger and rejects other
names) emits a `ci.triage.manual` event (P1 entry point);
`inspect ci-triage --message-ts <ts>` dumps a single audit row;
`inspect ci-triage` (no `--message-ts`) dumps the per-channel cursor state
(`slack_ci_alert_state`: `last_seen_ts`, `seeded`, age) plus a recent-audit
summary, so the trigger-quiet runbook step ("run `inspect ci-triage` and check for
a quarantine row") and the `ops doctor` liveness check read the same state.

## Data Model (migration `006_slack_ci_alert_state.sql`)

Linear, additive, never edited in place. Bumps `meta.schema_version` to `6`.

### `slack_ci_alert_state` — per-channel read cursor

```sql
CREATE TABLE IF NOT EXISTS slack_ci_alert_state (
    channel_id    TEXT NOT NULL PRIMARY KEY,   -- e.g. "C09SEN8MH5M"
    last_seen_ts  TEXT NOT NULL,               -- Slack message ts ("1718800000.001200"); high-water cursor
    seeded        INTEGER NOT NULL DEFAULT 0,   -- 0 until cold-start seed completes; 1 thereafter
    updated_at    TEXT NOT NULL                 -- ISO8601 UTC (poll time)
);
```

### `ci_triage_audit` — one row per posted/skipped/failed triage

```sql
CREATE TABLE IF NOT EXISTS ci_triage_audit (
    id                  INTEGER PRIMARY KEY,
    event_id            TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,  -- FK → events; ON DELETE CASCADE so app/prune.py:_prune_events DELETE cascades the audit child under PRAGMA foreign_keys=ON (matches 002 pr_review_audit / 005 jira_triage_audit verbatim)
    channel_id          TEXT NOT NULL,
    message_ts          TEXT NOT NULL,                        -- the alert message ts (idempotency key part 1)
    repo                TEXT,                                 -- "rebellions-sw/ssw-bundle"; NULL when no run link
    run_id              TEXT,                                 -- from actions/runs/<id>; NULL when not extractable
    pr_number           INTEGER,                              -- best-effort
    failed_jobs         TEXT NOT NULL DEFAULT '[]',           -- JSON array of failed job names
    status              TEXT NOT NULL CHECK (status IN (
                            'posted',
                            'skipped_no_run_link',
                            'skipped_not_ci_failure',
                            'skipped_already_triaged',
                            'skipped_log_unavailable',
                            'failed'
                        )),
    attribution         TEXT,                                 -- infra_env | product_regression | flaky | unknown (when posted)
    classification      TEXT,                                 -- infra|environment|test_failure|device_failure|build_failure|dependency|timeout|flaky|permission|unknown
    owner_area          TEXT,                                 -- DevOps|SysFw|SysSol|Connectivity|Driver|HW|Unknown
    confidence          TEXT,                                 -- low|medium|high
    wiki_matches        TEXT NOT NULL DEFAULT '[]',           -- JSON array of matched incident/SDOC paths
    posted_channel_id   TEXT,                                 -- dry_run channel OR original channel (thread reply)
    posted_message_ts   TEXT,                                 -- ts of the bot's posted message; NULL if not posted
    summary_chars       INTEGER,                              -- len(slack body) when posted
    persona_skill       TEXT,
    persona_mtime_ns    INTEGER,
    gh_error            TEXT,                                 -- short label when gh --log-failed failed
    wiki_error          TEXT,                                 -- short label when wiki pull/search failed (incl. wiki-less-degraded)
    error               TEXT,                                 -- error message when status='failed'
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cta_msg ON ci_triage_audit(channel_id, message_ts);
CREATE INDEX IF NOT EXISTS idx_cta_run ON ci_triage_audit(repo, run_id);
CREATE INDEX IF NOT EXISTS idx_cta_event ON ci_triage_audit(event_id);
CREATE INDEX IF NOT EXISTS idx_cta_status ON ci_triage_audit(status);

UPDATE meta SET value = '6' WHERE key = 'schema_version';
```

**Why two tables**: `slack_ci_alert_state` is **one row per channel**, updated
in place every poll (cursor advance). `ci_triage_audit` is **append-mostly**
(updated only on force-supersede) and persists triage history. Same rationale as
feature 002.

**FK cascade / retention**: `ci_triage_audit.event_id` FKs `events(id)`
**`ON DELETE CASCADE`** — this `ON DELETE` action is load-bearing, not cosmetic.
`PRAGMA foreign_keys=ON` is enforced on every connection (CLAUDE.md SQLite
contract), and `app/prune.py:_prune_events` does a literal
`DELETE FROM events WHERE created_at < ? AND id NOT IN (SELECT event_id FROM outbox)`.
A `NOT NULL` reference whose `ON DELETE` defaults to `NO ACTION` would raise a
foreign-key constraint violation and **abort the prune transaction** for any
events row that ever produced a `ci_triage_audit` child — silently wedging the
90-day retention prune. The `ON DELETE CASCADE` (copied verbatim from the
precedents this migration mirrors — `002` `pr_review_audit:23`, `005`
`jira_triage_audit:37`) lets the `DELETE FROM events` cascade the audit child so
audit rows prune with their events row, **no new prune logic**.
`test_migration_006` (and `test_ci_triage_audit`) MUST assert that deleting an
`events` row under `foreign_keys=ON` cascades its `ci_triage_audit` row — a
regression test for exactly the dropped-`ON DELETE` footgun. `slack_ci_alert_state`
rows are per-channel (2 rows), never pruned.

### Trigger state machine (per channel, mirrors the 5-case pattern)

The 5-case pattern from `gh_review_requested`/`jira_assigned` maps to a
**per-channel cursor** rather than a per-entity membership flag, since a Slack
channel is a stream, not a set. The trigger entry supplies `max_per_cycle`
(default 20) and `staleness_seconds` (default 21600 = 6 h):

```
For each channel C every poll:
  row = SELECT * FROM slack_ci_alert_state WHERE channel_id = C

  CASE 1: row IS NULL                          # first-ever poll of this channel (cold-start)
     page = conversations.history(C, limit=1)
     if page has NO messages (empty channel, or a brand-new channel):
        ⇒ do NOT INSERT a seed row this cycle (no ts to anchor to);
          log slack_ci_alert.cold_start_empty and retry seeding next poll.
          (Avoids a None-ts crash and avoids seeding to a fabricated cursor.)
     else:
        latest = page.messages[0].ts
        ⇒ INSERT (channel_id=C, last_seen_ts=latest, seeded=1)
        ⇒ NO EMIT  (no retroactive triage)
     # Note: the seed reads only limit=1 and emits nothing, so a brand-new alert
     # arriving in the same cold-start cycle becomes the seed cursor and is NOT
     # triaged — intentional (no-retroactive-triage). It IS triaged if it lands
     # AFTER the seed cursor on a subsequent poll, which is the steady-state path.

  CASE 1b: row.seeded=1 AND (now - last_seen_ts) > staleness_seconds   # large-gap guard
     # after a long outage (laptop sleep / deploy / AuthError-then-fix) we do NOT
     # back-fill hours of stale runs — a 6h-old CI failure does not need a fresh
     # first-pass. Re-seed the cursor to latest, like a cold start.
     latest = conversations.history(C, limit=1).ts
     ⇒ UPDATE last_seen_ts := latest
     ⇒ log slack_ci_alert.stale_cursor_reseed (gap_seconds)
     ⇒ NO EMIT

  CASE 2: row.seeded=1, page has messages with ts > last_seen_ts that ARE candidates
     ⇒ candidates := filter(messages, is_ci_failure_candidate), oldest→newest
     ⇒ for each candidate UP TO max_per_cycle: in ONE tx,
          insert_event(dedup_token) + enqueue_handler + UPDATE last_seen_ts := candidate.ts
     ⇒ if len(candidates) > max_per_cycle:
          log slack_ci_alert.max_per_cycle_hit (cap, collected);
          advance last_seen_ts ONLY to the last EMITTED candidate's ts
          (remainder is re-read next cycle — no message skipped)
       else (all emitted):
          advance last_seen_ts := max(last emitted candidate.ts, newest non-candidate ts in page)
          (so trailing chatter newer than the last candidate is consumed — see CASE 3)
     ⇒ EMIT one event per emitted candidate

  CASE 3: row.seeded=1, new messages exist but NONE are CI-failure candidates
     ⇒ UPDATE last_seen_ts := newest.ts (advance past pure chatter)
     ⇒ NO EMIT

  CASE 4: row.seeded=1, no new messages (ts > last_seen_ts)
     ⇒ UPDATE updated_at only
     ⇒ NO EMIT

  CASE 5: PAUSE active
     ⇒ pause_check() true ⇒ skip the Slack read entirely this cycle:
        NO API call, NO cursor move, NO emit. The cursor advances on the next
        UNPAUSED poll, which then processes the gap from last_seen_ts forward
        exactly once (subject to CASE 1b staleness if the pause was long).
        (matches gh_review_requested.py:120-121 / jira_assigned.py:105-108 —
        both sleep one interval without touching the API on pause.)
```

All INSERT/EMIT/cursor-advance for one candidate happen in one `aiosqlite` tx.
Failure on one channel doesn't block the other.

**Commit boundary — per-candidate, NOT per-cycle (tasks.md note).** The
`jira_assigned` precedent commits **once per poll cycle** after its whole loop
(`jira_assigned.py:196`). `slack_ci_alert` deliberately diverges: it commits **per
candidate** (insert_event + enqueue_handler + cursor `UPDATE last_seen_ts` in one
tx, committed before the next candidate). Both are valid against the outbox
primitives — `insert_event` / `enqueue_handler` do not self-commit (verified
`outbox.py:47-95`) so the commit boundary is the caller's choice — but
**per-candidate is the correctness-required choice here** for the `max_per_cycle`
partial-advance semantics: it guarantees the cursor only ever advances to the
**last EMITTED** candidate, so a mid-cycle crash leaves the unemitted remainder
re-readable next cycle with no skip and no double-emit. An implementer must **not**
blindly copy `jira_assigned`'s per-cycle commit boundary, which would advance the
cursor past candidates whose events never committed. `tasks.md` carries this as an
explicit instruction on the trigger task.

**Cursor-advance note (CASE 2/3 reconciliation):** to avoid re-reading trailing
non-candidate chatter every poll until a newer message arrives, CASE 2 advances
`last_seen_ts` to `max(last emitted candidate ts, newest non-candidate ts in the
fully-consumed page)`; CASE 3 advances past pure chatter. When `max_per_cycle`
truncates, the cursor stops at the last emitted candidate so the unemitted
remainder (and any chatter after it) is re-read next cycle. This bounds the
re-read window without ever skipping a candidate.

**PAUSE contract (reconciled with spec Acceptance 2.6):** the plan's behavior —
skip the read, do **not** advance the cursor, emit nothing on a paused cycle — is
canonical because it matches both shipped precedents (`gh_review_requested.py` /
`jira_assigned.py`) and yields the exactly-once-after-unpause guarantee.
Spec Acceptance 2.6 as originally written ("state 커서는 갱신되나…") contradicts
this; it must be corrected to "PAUSE 중에는 Slack read 자체를 건너뛰어 커서가
갱신되지 않고, unpause 후 다음 poll에서 last_seen_ts 이후 메시지를 누락 없이
정확히 1회씩 처리한다." The `contracts/slack-web-api-surface.md` PAUSE note must
carry the same wording so the Acceptance-2.6 test asserts the precedent-aligned
behavior, not a cursor advance. (Resolution per the daeyeon-bot invariant
tie-breaker: when spec and shipped precedent conflict on delivery semantics, the
precedent + CONTRACTS at-least-once model win, and the spec text is fixed.)

### Dedup token

| Source | `events.source_dedup_key` formula |
|---|---|
| `slack_ci_alert` (auto) | `sha256("slack-ci-alert\|{channel_id}\|{message_ts}").hexdigest()` |
| `manual` (force=false) | `sha256("manual-ci-triage\|{repo}\|{run_id}").hexdigest()` |
| `manual` (force=true)  | `sha256("manual-ci-triage\|{repo}\|{run_id}\|{unix_ts}").hexdigest()` |

The existing `UNIQUE(source, source_dedup_key)` on `events` makes a re-emit a
no-op at the SQL layer (covers replay/restart). Note the auto path
(`channel_id|message_ts`) and the manual non-force path (`repo|run_id`) live in
**different keyspaces**, so the SQL `UNIQUE(source, source_dedup_key)` only dedups
*within* a source. The cross-source "same run reached via two different alerts"
case is caught only by the secondary guard below.

**Secondary `repo+run_id` guard**: before posting, the handler queries
`ci_triage_audit` for an existing `status='posted'` row with the same
`(repo, run_id)` and skips (`skipped_already_triaged`) when found and `force` is
not set — so two distinct alerts pointing at the same run triage once. Two facts
about this guard, stated explicitly so an implementer does not "fix" them:
- **Correctness depends on `concurrency=1`** (per the `ci_triage` manifest). The
  guard is a pre-post audit *lookup* (not a SQL `UNIQUE` constraint), so two
  events for the same run only collapse to one post because `concurrency=1`
  serializes them — the second claim runs after the first has committed its
  `status='posted'` audit row and therefore sees it. If concurrency were ever
  raised, this guard would need to become a real constraint or a claimed-row lock.
- **NULL `(repo, run_id)` rows must NOT collapse.** `skipped_no_run_link` /
  `skipped_log_unavailable` rows have `repo` and `run_id` both NULL. SQL
  `NULL = NULL` is never true, so `idx_cta_run(repo, run_id)` and the guard query
  correctly never match two link-less alerts to each other — which is the desired
  behavior (two independent link-less alerts are two independent triages). This
  is intentional, not a bug; `data-model.md` states it so no one "fixes" it into
  matching all-NULLs together.

### At-least-once residual (post-then-audit window)

The cross-event idempotency guards above dedup *separate* events. Within a single
event the order is **post `chat.postMessage` → write `ci_triage_audit` row /
Ack** (this is exactly the sanctioned `jira_triage.py:455-458` /
`pr_review` order). If `chat.postMessage` succeeds but the process crashes before
the audit INSERT commits, a re-claim re-posts — a **visible duplicate thread
reply**. This is the **same residual as feature 002**, not a new one: Slack has
no idempotency key, so `side_effect_key=None` is correct (there is nothing to
set). FR-011's "exactly 1 post" is the steady-state guarantee; the crash-window
duplicate is the documented residual. The runbook calls this out (see §Rollout /
Ops) so on-call recognizes a rare double-post as a known crash artifact rather
than a bug. (A pre-post "posting" audit row with post-reconcile was considered
and rejected for v1: it adds a second write per event for a window that the
shipped handlers already accept; revisit only if duplicates are observed in
practice.)

## Event / Contract Surface

| `type` | Source | Payload (JSON) |
|---|---|---|
| `slack.ci_alert` | `slack_ci_alert` (auto) | `{channel_id, message_ts, author_id, raw_blob}` (raw_blob = merged text+attachments[]+blocks for the handler to re-parse deterministically) |
| `ci.triage.manual` | `manual` (CLI) | `{repo, run_id, dry_run_channel: str\|null, force: bool}` |

**`raw_blob` is treated as non-secret-bearing (v1 invariant; state this in
`data-model.md`).** `events.payload_json` is written by
`outbox.insert_event()` straight to SQLite (`outbox.py:47-75`) — the
`infra/logging.py` redaction processor runs on **log records, not persisted event
payloads**, so anything stored in `raw_blob` is *not* scrubbed at rest. This is
acceptable for v1 only because the three alert sources (sukju-bot /
dev_syssw_test / SSW-Alert-Bot) emit benign CI-failure text with no secret
material, and the handler **re-parses `raw_blob` deterministically rather than
re-fetching** (so the stored blob is the single source the handler reads).
`data-model.md` MUST state this invariant explicitly so no one later routes a
secret-carrying alert source through the same field without adding a payload
redaction step. **Cross-reference to add in `data-model.md` (SSE concern):** the
two payload classes have *different* redaction coverage and the asymmetry is
deliberate — error-anchored `--log-failed` windows logged at INFO **DO** pass
through the `infra/logging.py` redaction processor (they are log records, scrubbed
before any sink), whereas `events.payload_json` / `raw_blob` does **NOT** (it is
persisted straight to SQLite, not a log record). State both explicitly so a future
maintainer does not route `--log-failed` output (secret-bearing) through
`raw_blob` and lose the scrub. (The redacted-at-sink path still protects daemon
logs of the `--log-failed` payload — that is a separate, log-record code path,
covered by the FR-006 regression test.)

**Routing** (added to `config.example.toml`):
```toml
[routing]
"slack.ci_alert"     = ["ci_triage"]
"ci.triage.manual"   = ["ci_triage"]
```

**HandlerResult / typed-error mapping** (centralized in `dispatcher._run_one`;
handler returns results directly):

| Condition | Result |
|---|---|
| Persona missing/invalid | `DeadLetter("persona unavailable: …")` |
| Not a CI-failure candidate (manual edge / re-parse miss) | `Ack` + audit(`skipped_not_ci_failure`) |
| No `actions/runs` run link AND no Loki window | `Ack` + audit(`skipped_no_run_link`) — minimal "no machine-readable run/Loki link; manual triage needed" note may be posted (see §Pipeline) |
| Already triaged `(channel_id,message_ts)` or `(repo,run_id)`, force=false | `Ack` + audit(`skipped_already_triaged`) |
| `gh --log-failed` transient (HTTP 5xx / network / timeout) | `Retry(DEFAULT_BACKOFF_S)` (record `gh_error`) |
| `gh --log-failed` run/log **unavailable** (not found / deleted / logs expired by retention / re-run overwrote — no HTTP code) → `RunLogUnavailableError` | `Ack` + audit(`skipped_log_unavailable`, `gh_error`) — **NOT `Retry`** (the log is gone; re-queueing wastes the slot). If a Loki window is present, fall through to the device-level Loki path first, then skip. |
| Wiki `pull` transient, existing clone present | continue with stale clone; record `wiki_error`; do NOT skip/retry |
| Wiki initial clone fails, no prior clone | **wiki-less triage** (no handler-level retry): proceed with `confidence` capped `low`, record `wiki_error`. (The handler has no attempt counter — see note below.) |
| Claude transient (SDK raises `TransientError`/`RateLimitError`/`QuotaError`) | dispatcher `Retry` (up to `MAX_TRANSIENT_ATTEMPTS=10`) |
| Claude malformed output (in-call 2-attempt loop both fail) | `DeadLetter("ci triage returned malformed output")` (the 2-attempt loop is **internal to one `handle()` call** — see note) |
| Redaction match in Slack body | `DeadLetter("redaction would alter posted content")` |
| Slack `invalid_auth`/`token_revoked` | `AuthError` → daemon halt (exit 78) |
| Slack `ratelimited` (429) | `Retry(RATE_LIMIT_BACKOFF_S)` |
| Slack 5xx / timeout | `Retry(DEFAULT_BACKOFF_S)` |
| Post target not in channel allowlist | **boot**: `ConfigError` → exit 78 (validated in `container.build`; primary guard — fails loud before any alert). **handler-time** (effectively unreachable; static config): `ConfigError` subclasses `PermanentError` ⇒ `DeadLetter` (no halt, no post). |
| `asyncio.wait_for` budget exceeded (any attempt) | handler **always** raises `TransientError` → dispatcher `Retry`; dispatcher promotes to `DeadLetter` only at `MAX_TRANSIENT_ATTEMPTS=10` (mirrors `jira_triage.py:233-240`). The handler does **not** distinguish 1st vs 2nd timeout — it has no attempt counter. |
| Post OK | `Ack` + audit(`posted`, posted_message_ts, attribution, classification, owner_area, confidence) |

**Cross-invocation vs in-call retry (the one source of confusion in earlier
drafts — pinned here so the implementation and its tests do not diverge from this
table).** `HandlerContext` (`core/protocols.py:26-31`) exposes only
`clock` / `trace_id` / `claude_session_factory` — it does **NOT** surface the
outbox row's `attempt` field, and CONTRACTS forbids the handler counting attempts
or running its own DeadLetter ladder (the exception→result mapping is centralized
in `dispatcher._run_one`). So the table distinguishes two retry kinds:

- **Cross-invocation (dispatcher-driven) retries** — the timeout and Claude-SDK
  rows. The handler raises the typed `core.errors` exception **on every
  occurrence** (it cannot know "which try" it is on); the dispatcher's
  `_classify_transient` counts via the outbox `attempt` field and promotes to
  `DeadLetter` at `MAX_TRANSIENT_ATTEMPTS=10` (`dispatcher.py:48,272`). There is
  **no** "timed out twice → DeadLetter" — that framing was un-architectural and is
  removed.
- **In-call (single-`handle()`) retries** — the **Claude malformed-output** row
  only. Re-prompting once for a parseable `TriageOutput` is a `for attempt in
  range(2)` loop **inside one `handle()` call** (exactly `jira_triage.py:662-701`),
  so "2nd try" here means the second iteration of an in-process loop, not a second
  dispatcher invocation. If both in-call attempts fail to parse, the handler
  returns/raises so the result is `DeadLetter` — a single, deterministic decision
  made within one invocation, which the architecture supports.

The **wiki initial-clone failure** row was likewise rewritten: there is **no
handler-level "Retry once then fall through"** (that would require the attempt
counter the handler lacks). The chosen rule is the architecturally sound,
degraded-but-honest path the plan already endorses — on initial-clone failure with
no prior clone, go **straight to wiki-less triage** with `confidence` capped at
`low` and `wiki_error` recorded. (A transient *network* blip is therefore absorbed
as a one-shot low-confidence triage, not a DeadLetter and not an attempt-counted
retry — see §`infra/oncall_wiki.py` for why this is preferred over raising
`TransientError` and letting it retry up to 10 times: a first-run network blip
should produce a useful low-confidence first-pass immediately, not stall the
queue slot through ten backoff cycles.)

## Pipeline

```
event (slack.ci_alert OR ci.triage.manual)
  │
  ├─ load persona (mtime stat → reload on change)           # invalid ⇒ DeadLetter
  │
  ├─ parse alert  (alert_parse.merge + extract_run_ref      # auto: re-parse raw_blob
  │   + extract_pr_meta + extract_loki_window)              # manual: repo/run from CLI
  │     ├─ no run link AND no Loki window ⇒ Ack + skipped_no_run_link
  │     │     (optionally post a minimal "first-pass: no machine-readable
  │     │      run/Loki link found, manual triage needed" note to the same
  │     │      target so on-call knows the bot saw it; gated by
  │     │      [handlers.ci_triage].post_no_evidence_note, default false)
  │     └─ not a candidate ⇒ Ack + skipped_not_ci_failure
  │
  ├─ idempotency guard (audit lookup on (channel_id,message_ts) then (repo,run_id))
  │     └─ found + force=false ⇒ Ack + skipped_already_triaged
  │
  ├─ EVIDENCE (dual path):
  │     ├─ run-link path:  gh run view <run_id> --repo <repo> --log-failed
  │     │     → ci_triage_parsing.strip_ansi                # MUST run before redact
  │     │     → infra/logging redaction (reused: xox*/gh PAT/AWS/entropy)
  │     │     → error-anchored truncation: windows around
  │     │       `##[error]` / `Process completed with exit code` / `ERROR -` /
  │     │       `FAIL` / "test failed"; per-failed-job windows; NO head/tail.
  │     │       Full text (438 KB measured) is NOT dumped whole to the daemon
  │     │       local log: only the redacted error-anchored windows are logged
  │     │       at INFO; the full redacted payload is gated behind DEBUG and
  │     │       relies on journald/launchd log rotation, so a release-branch
  │     │       alert storm cannot balloon the local log. Never to Slack.
  │     └─ device-level path: if run link absent/insufficient (e.g.
  │           "0x50555746 device unreachable" CP/SMC FW fail), use the
  │           extracted host+window to query Loki (LokiClient built in the
  │           ci_triage container branch — works with feature 002 OFF) for
  │           fwlog/smclog/kernel/syslog (reference_loki labels).
  │           No extractable host+window ⇒ skip device path, degrade to
  │           confidence=low (not a crash).
  │
  ├─ oncall_wiki.ensure_fresh()  (git pull --ff-only; read-only)
  │     ├─ transient + existing clone ⇒ stale clone + wiki_error (no skip)
  │     ├─ initial clone fails, no prior clone ⇒ wiki-less, conf≤low (NO retry —
  │     │     handler has no attempt counter; permanent remote failure caught at
  │     │     boot by ls_remote() probe)
  │     └─ search(signatures=log error sigs, phrases=multiword error spans):
  │           signature: frontmatter first → body → weight multiword phrases;
  │           scope incidents/*.md; ALWAYS include recovery-playbook.md.
  │
  ├─ Claude SDK session (ctx.claude_session_factory()) with 3 inputs:
  │     (1) alert metadata  (channel/repo/PR#/head SHA/failed jobs/run URL/
  │         consecutive-fail-N/prior-similar-PR — as far as the source gives)
  │     (2) error-anchored failed log (+ Loki slices on device path)
  │     (3) wiki snippet (signature-matched incidents + recovery-playbook)
  │     Prompt: log = primary, wiki = supporting; no over-assertion;
  │     insufficient ⇒ attribution=unknown / confidence=low.
  │     → parse into TriageOutput; malformed → ONE in-call re-prompt
  │       (for-loop inside this single handle() call, mirrors
  │       jira_triage.py:662-701) → if still malformed ⇒ DeadLetter.
  │       (This is an in-call loop, NOT a dispatcher retry — see HandlerResult note.)
  │
  ├─ render Slack summary (summary-only; no full log / no prompt):
  │     **Pinned field ORDER — actionable header first, never truncated:**
  │       line 1 (header): attribution · owner_area · confidence
  │       line 2:          recommended_action · rerun_advice
  │       line 3:          repo · PR · failed check/job · run link
  │       then (truncatable body): triage summary · classification ·
  │                                wiki match (SDOC/incident link) · likely_cause
  │       footer:          "🤖 automated first-pass (daeyeon-bot)" + run/wiki links
  │     **Hard char budget** (pinned in contracts/claude-triage-output.md):
  │       header lines 1-2 are guaranteed-present; only the BODY block is
  │       mid-truncated to fit the total budget, so a verbose Claude run can
  │       never lop off the actionable verdict. On-call can act from the first
  │       3 lines + footer links alone.
  │
  └─ POST (single write; channel-allowlist guarded):
        PoC:   chat.postMessage(dry_run_channel, …)            # one-way test channel
        Promo: chat.postMessage(original channel, thread_ts=message_ts)   # alert thread reply
        identity: username="CI Triage", dedicated icon (chat:write.customize)
        → Ack + audit(posted, …)    # post-then-audit; see §At-least-once residual
```

### Output schema (`ci_triage_schemas.TriageOutput`, Pydantic v2)

```
attribution    : Literal["infra_env","product_regression","flaky","unknown"]   # top-level judgement
classification : Literal["infra","environment","test_failure","device_failure","build_failure","dependency","timeout","flaky","permission","unknown"]
owner_area     : Literal["DevOps","SysFw","SysSol","Connectivity","Driver","HW","Unknown"]   # == wiki domain enum (see note)
confidence     : Literal["low","medium","high"]
summary        : str (min_length=1)
log_evidence   : tuple[Evidence, ...]    # quote + citation; required when attribution != "unknown"
wiki_matches   : tuple[WikiRef, ...]     # incident/SDOC path + why; empty allowed
likely_cause   : str
known_remedy   : str | None              # from recovery-playbook when matched
recommended_action : str
rerun_advice   : Literal["safe_to_rerun","do_not_rerun","needs_investigation","unknown"]
needs_human    : bool
```

A `@model_validator` enforces two rules (mirrors spec Acceptance 3):
1. **evidence required when `attribution != "unknown"`** — at least one
   `log_evidence` entry.
2. **confidence floor by anchor strength** — `confidence` may not be `"high"`
   when `wiki_matches` is empty **and** only a *weak* anchor was matched.
   **Anchor strength is defined concretely** (and pinned in
   `contracts/claude-triage-output.md`): a **strong** anchor is a multi-word
   error phrase matched against an incident `signature:` (e.g. `"VM creation
   failed"`, `"rsync … golden-base"`); a **weak** anchor is a lone generic
   token (`FAIL`, `qemu`, `ERROR`). With no wiki match and only weak anchors,
   `confidence` is capped at `low`; a single strong signature match permits
   `medium`. This prevents the model self-reporting `high` off a lone `FAIL`
   line.

**`owner_area` ↔ wiki domain enum (verification gate):** the `owner_area`
`Literal` is asserted to equal the `ssw-devops-oncall` vault `domain` frontmatter
vocabulary. Because a silent drift between the Pydantic `Literal` and the vault
would mis-route attribution, `contracts/oncall-wiki-surface.md` MUST pin the
exact `domain:` vocabulary read from a real incident file in the live vault, and
a `test_ci_triage_schemas` case must assert the `Literal` matches that pinned
list. If the vault uses a value not in the current `Literal`
(`DevOps|SysFw|SysSol|Connectivity|Driver|HW|Unknown`), the `Literal` is the
thing that changes (the vault is the source of truth). **Maintenance note**: the
test reads the *pinned* list in `contracts/oncall-wiki-surface.md` (for
hermeticity — it does not hit the live vault), so a real vault drift is caught
only when someone re-pins that contract. This is an accepted v1 tradeoff and a
recurring maintenance touchpoint (re-pin on vault domain changes); listed under
§Risks.

## Posting Identity (DECIDED — bot token only)

The user-token path is **dead**: the workspace has App approval ON and admins
will not approve user-token apps (verified 2026-06-19), so the `daeyeon`
user-token route is removed from the design — do not build for it.

Posting uses the existing **`dev_syssw_test` bot token (`xoxb-`)** with scopes
`chat:write` + `chat:write.public` + `chat:write.customize` + `channels:history`,
already invited to both channels. Consequences and mitigations baked into the
plan:

- **Identity**: triage posts under the bot account. `chat:write.customize` sets a
  dedicated `username="CI Triage"` + icon and the message footer
  `🤖 automated first-pass (daeyeon-bot)` so readers immediately see it is an
  automated first-pass, not a human verdict.
- **Attribution preserved in the BODY, not the poster**: the on-call ownership
  judgement is carried by the `owner_area` / `attribution` fields **in the
  message text**, not by the Slack author identity. The oncall vault's
  attribution model (infra/env → oncall-owned + SDOC; product regression → PR
  author routing) is reflected in `attribution`, not in who posted.
- **Reads** require channel membership (`not_in_channel` otherwise) — bot
  invited to both; **posts** can use `chat:write.public` without joining, but the
  thread-reply target is one of the same two channels anyway, and the
  **channel-allowlist post guard** (see §`infra/slack.py`) refuses any target
  outside {2 known channels, dry_run_channel} so `chat:write.public` cannot be
  abused by a fat-fingered config.

## Phased PR Slices

Mirrors how feature 002 phased PR-1.. — each slice independently shippable,
landing behind `enabled=false`.

### P1 — manual-fire vertical slice (User Story 1)
The whole pipeline minus Slack polling, driven by
`dev fire-ci-triage --repo <r> --run <id> [--channel <test>]`. De-risks P2.
- Migration `006` (incl. `skipped_log_unavailable` status) + `slack_ci_alert_state`/`ci_triage_audit` CRUD.
- `infra/slack.py` (`auth_test`, `post_message` with customize + channel-allowlist guard + dry_run channel).
- `container.build`: `load_secret("slack_bot_token")` (**catch load-time `AuthError` → re-raise `ConfigError("…not configured…")` for doctor clarity**; the `auth.test`-time `AuthError` propagates as-is) + `register_literal_secret(token)` + Slack `auth.test` probe (gated on enabled) + assemble post allowlist + **boot-time allowlist validation of `dry_run_channel`/`post_target` (ConfigError→exit 78)** + **`oncall_wiki.ls_remote()` git-reachability probe (gated on enabled, ConfigError/AuthError→exit 78)** + **build/share a `LokiClient` in the ci_triage deps branch (gated on ci_triage enabled; `ConfigError` at boot if `[loki]` absent)**. **Ordering: the `oncall_wiki.ls_remote()` probe runs AFTER the 0600 managed known_hosts file is created** (so the probe's own SSH does not fail on a missing known_hosts file) — see §`infra/oncall_wiki.py`.
- `infra/alert_parse.py` (run-ref extraction tested on the 3 real shapes).
- `gh_cli.run_view_log_failed` **with the dedicated non-`api` classifier** (`RunLogUnavailableError` for gone/expired logs → skip, not retry) + `ci_triage_parsing` (ANSI strip → redact → anchor truncate; ordering asserted).
- `infra/oncall_wiki.py` (clone + pull --ff-only + **headless-safe git env: `GIT_TERMINAL_PROMPT=0` + `GIT_SSH_COMMAND` BatchMode + 0600 managed known_hosts** + two-layer path guard + initial-clone-failure rule + **partial/corrupt-clone detection & re-clone** + signature ripgrep).
- `handlers/ci_triage.py` + `ci_triage_schemas.py` + persona SKILL.md.
- `ci.triage.manual` event + routing + registry wiring + `cli/dev.py` (`fire-ci-triage` subcommand).
- **Ships posting to `dry_run` channel only.** `[handlers.ci_triage].enabled=false`.
- Independent test = spec Acceptance 1–5 (US1).

### P2 — polling trigger (User Story 2)
- `triggers/slack_ci_alert.py` (cursor poll, cold-start seed, candidate filter, `max_per_cycle` cap, `staleness_seconds` re-seed).
- `slack.history` cursor pagination.
- Registry `slack_ci_alert` branch + `SlackCiAlertDeps` + supervised-run-loop wiring (`TriggerSupervisor` + `PermanentFailureReporter`) + `app/prune.py` (minimal/none).
- `slack.ci_alert` event + routing.
- `[triggers.slack_ci_alert].enabled=false` (flip to enable).
- **Spec/contract reconciliation lands in THIS slice (same PR)**: edit
  `spec.md` Acceptance 2.6 and `contracts/slack-web-api-surface.md` to the
  precedent-aligned PAUSE wording (skip read, no cursor move, exactly-once after
  unpause) so the Acceptance-2.6 test asserts the shipped behavior and spec/test
  do not drift. (See §Trigger state machine PAUSE contract.)
- Independent test = spec Acceptance 1–7 (US2): cold-start no-retroactive, filter,
  dedup on replay/restart, **PAUSE skip-read no-cursor-move** (reconciled
  Acceptance 2.6), `max_per_cycle` cap + staleness re-seed, `repo+run_id`
  secondary dedup.

### P3 — posting promotion (R2)
Config toggle `[handlers.ci_triage].post_target = "dry_run" | "thread"`. After
`dry_run` validation, flip to `thread` to post the reply in the original alert
thread (`thread_ts = message_ts`). No code change beyond the toggle branch (which
P1 already implements behind the `post_target` switch) — this slice is the
operational promotion + runbook note + the `force`-supersede "Updated triage
(supersedes …)" header path. The channel-allowlist guard already covers the
`thread` target (the 2 known channels are in the allowlist), so the flip cannot
broadcast outside them.

**Evidence-based promotion gate (mandatory runbook step, not a date).** Promotion
to `thread` is the single biggest channel-noise risk: once live, *every*
candidate — including `attribution=unknown` / `confidence=low` — gets a reply in
the channel on-call lives in, and the only v1 lever is disabling the whole handler
(a confidence-gated / per-source mute is deferred to v2 — see §Risks). To keep the
flip evidence-based rather than calendar-based, the P3 runbook step **mandates
reviewing the `dry_run` audit rows' `attribution`/`confidence` distribution before
flipping** (e.g. `inspect ci-triage` aggregate, or a SQL summary over
`ci_triage_audit` status/attribution/confidence): if a non-trivial share are
`unknown`/`low`, hold the flip and tune the persona first. **At this step
`post_no_evidence_note` is flipped to `true` as the P3 default** (not merely
"consider"): once posting lands in the channel on-call lives in, "bot saw it, GH
log expired / no machine-readable link" is itself useful triage signal, and silence
is indistinguishable from "bot ignored a real failure". So the P3 promotion runbook
step **sets `post_no_evidence_note=true` together with `post_target=thread`** —
both flips happen at promotion. (It stays `false` while `post_target=dry_run` so
the PoC test channel is not noisy.)

**Force-supersede leaves the prior post.** This slice ships the
`Updated triage (supersedes earlier comment at HH:MM:SS)` header path. The Slack
adapter has **no `chat.update`/`chat.delete`** (read-only-plus-`chat.postMessage`
only), so a force posts a *new* message and the prior one stays — repeated force on
a flapping run stacks replies in one thread. Documented in the runbook so on-call
expects it.

## Configuration

New config sections in `config.example.toml` (copied to local `config.toml` to
enable). Typed in `app/config.py` mirroring the `JiraConfig` / `*TriggerEntry` /
`*HandlerEntry` idiom:

```toml
[slack]
# bot token is NOT here — it is a secret (key name `slack_bot_token`, see Secrets).
api_base = "https://slack.com/api"
timeout_seconds = 20

# [loki] is the SAME section feature 002 already defines (do not duplicate if
# present). It is REQUIRED when [handlers.ci_triage].enabled = true — the
# device-level dual-evidence path builds a LokiClient from it even when
# jira_triage is disabled. container.build raises ConfigError at boot if
# ci_triage is enabled and [loki] is absent.
[loki]
base_url = "http://loki.ssw.rbln.in"
timeout_seconds = 20
per_stream_max_bytes = 1048576

[oncall_wiki]
remote_url = "git@github.com:rebellions-sw/ssw-devops-oncall.git"
clone_path = "var/ssw-devops-oncall"     # relative → resolved under project_root
allow_external = false                    # path guard escape hatch; hard-ban on ~/ssw-devops-oncall still applies

[triggers.slack_ci_alert]
enabled = false
channels = ["C09SEN8MH5M", "C0A406KREHF"]   # #ssw-devops-alerts, #ssw-devops-help
poll_interval_seconds = 120
max_per_cycle = 20                           # per channel per cycle emit cap (mirrors jira_assigned)
staleness_seconds = 21600                    # >6h gap ⇒ re-seed cursor instead of back-filling stale runs

[handlers.ci_triage]
enabled = false
persona_skill = "daeyeon-bot-ci-triage"
dry_run_channel = "C0XXXXXXXXX"             # PoC test channel; part of the post allowlist
post_target = "dry_run"                      # "dry_run" | "thread" (P3 promotion)
post_no_evidence_note = false                # if true, post a minimal note when no run/Loki link found
```

`max_per_cycle` and `staleness_seconds` are passed by `container.build` into the
`SlackCiAlertTrigger` constructor (not `TriggerManifest`). The post allowlist is
built as `tuple(channels) + (dry_run_channel,)` and handed to `SlackClient`.

## Testing Strategy

Fakes before mocks (`tests/fakes/` is a real package). Coverage targets from
`CLAUDE.md`: core/app ≥ 90 %, infra ≥ 80 %, cli ≥ 60 %.

| Slice | Unit | Integration |
|---|---|---|
| P1 | `test_alert_parse` (3 real shapes incl. SSW-Alert-Bot `attachments[].text`), `test_ci_triage_parsing` (438 KB fixture → anchored windows, ANSI stripped, **strip→redact ordering**, GITHUB_TOKEN + AWS key fully scrubbed before local-log sink AND before handing to Claude), `test_oncall_wiki` (fixture vault: two-layer path guard both fire, pull --ff-only, initial-clone-failure rule, **partial/corrupt-clone detected→re-cloned**, **git env carries `GIT_TERMINAL_PROMPT=0`+`BatchMode=yes` and known_hosts is 0600**, signature-first ranking, recovery-playbook always present), `test_gh_cli_log_failed` (**not-found / logs-expired stderr → `RunLogUnavailableError`→skip, NOT `TransientError`→infinite Retry; auth stderr still → `AuthError`**), `test_container_boot_validation` (**fat-fingered `dry_run_channel`/`post_target` → ConfigError at boot; `ls_remote` unreachable → fail-loud; ci_triage boots cleanly with `jira_triage` DISABLED and `CiTriageDeps.loki` is present; ci_triage enabled + `[loki]` absent → ConfigError; missing token → ConfigError "not configured" message; rejecting `auth.test` → AuthError "rejected the configured token" message; known_hosts 0600 file exists BEFORE `ls_remote()` runs**), `test_ci_triage_schemas` (enums + evidence-required validator + anchor-strength low-confidence path + owner_area==vault-domain assertion), `test_slack` (post_message customize + dry_run channel + **channel-allowlist rejection** via httpx MockTransport), `test_ci_triage_audit` (incl. **events-row delete cascades the audit row under `foreign_keys=ON`** — the retention-prune regression), `test_migration_006` (schema_version→6 + **FK declared `ON DELETE CASCADE`**), `test_ci_triage_handler` (FakeSlack/FakeGh/FakeOncallWiki/FakeClaudeSession; skip cases; dry_run post) | `test_ci_triage_e2e` (real aiosqlite + real git wiki fixture + fakes; manual-fire → audit row + captured FakeSlack post) |
| P2 | `test_slack_ci_alert_state` (cursor advance + cold-start seed flag), `test_slack_ci_alert_trigger` (FakeClock + FakeSlack: cold-start emits nothing, **empty-channel cold-start seeds no row + does not crash on None ts**, candidate filter, emit-on-new-candidate, advance-past-chatter, **max_per_cycle cap truncates + advances only to last emitted**, **staleness re-seed on large gap**, **PAUSE skips read with no cursor move**) | extend `test_ci_triage_e2e`: trigger emit → dispatcher claim → handler post; replay/restart dedup; `repo+run_id` secondary dedup; PAUSE skips read |
| P3 | `test_ci_triage_handler`: `post_target="thread"` uses `thread_ts`; force-supersede header | promotion smoke against fixture |

Redaction regression (FR-006 / secrets discipline): a unit test asserting
`infra/logging.py` scrubs an `xoxb-…` Slack token **and** a `GITHUB_TOKEN`/PAT
**and** an AWS key in a realistic `--log-failed` record, before any sink and
before the prompt is built — and that `strip_ansi` runs first so an ANSI-split
token is still caught. **Plus a defense-in-depth case**: a non-canonical /
rotated token form (a value WITHOUT the `xoxb-` prefix, e.g. a config-app token)
is still scrubbed because `container.build` called
`register_literal_secret(slack_token)` — proving redaction does not rest solely
on the `xox[baprs]-` shape regex. The test **asserts the post-`container.build`
state** (i.e. that the literal registration fires at the container/boot layer,
where `ssh_logs`'s weak-shape secret is also registered — verified
`logging.py:49`), not merely that the adapter holds the token: the registration
must be active before any log sink, so a stray log of the raw token *anywhere*
after boot is scrubbed. (The test calls `clear_literal_secrets_for_testing()`
between cases — see Test hygiene below.) (Implementer note: register in `container.build` right
after `load_secret("slack_bot_token")`, gated on enabled, not inside
`infra/slack.py`.)

**Test hygiene (DevOps concern):** `_LITERAL_REDACTIONS` is a **process-global**
list with no dedupe (`logging.py:46`). The redaction regression test (and any test
that drives `container.build` to register a literal) MUST call
`clear_literal_secrets_for_testing()` between cases, or registered literals leak
across tests and produce false passes (a later case "passes" only because an
earlier case's literal is still registered). `tasks.md` carries this as an
explicit test-hygiene note on the redaction-test task. Note also
`register_literal_secret` silently no-ops for values < 3 chars (`logging.py:58-60`)
— an `xoxb-` token is always long enough, so the canonical case is fine; the plan
does **not** over-promise literal redaction as a guarantee for arbitrary
sub-3-char token forms.

## Rollout / Ops

- **Landing**: `[handlers.ci_triage].enabled=false` and
  `[triggers.slack_ci_alert].enabled=false` — opt-in, like the other two
  handlers. P1 posts only to `[handlers.ci_triage].dry_run_channel`; promote to
  thread reply via `post_target` after validation (R2). **Boot is unaffected
  when both are disabled**: `container.build` only `load_secret("slack_bot_token")`
  + runs `auth.test` when the trigger or handler is enabled, so an operator who
  has not set the token can still boot the daemon (mirrors how feature 002 gates
  its Jira probe on `triage_enabled`).
- **PAUSE**: respected — the PAUSE flag-file blocks Claude calls and posting; the
  trigger **skips the Slack read on a paused cycle (no API call, no cursor
  move)**, so unpause processes the gap from `last_seen_ts` forward exactly once
  (subject to the staleness re-seed for very long pauses). This is the
  precedent-aligned behavior; spec Acceptance 2.6 is corrected to match (see the
  state machine PAUSE note).
- **Backlog after downtime**: `max_per_cycle` (default 20 per channel/cycle)
  bounds emit fan-out so a multi-hour outage cannot flood the queue; the
  `staleness_seconds` re-seed (default 6 h) skips back-filling stale runs
  entirely after a long gap. Both are first-class config knobs, not just a Risk.
- **Secrets**: one new key — **keychain account / file sibling name
  `slack_bot_token`**, env form **`SLACK_BOT_TOKEN`** (snake_case → upper per
  `secrets.py` convention) — an `xoxb-` token via the Keychain → 0600 file →
  env(`--insecure-env`) chain; `container.build` `load_secret`s it,
  `register_literal_secret(...)`s it for defense-in-depth redaction (so a
  non-canonical/rotated form is still scrubbed by literal match), and runs an
  `auth.test` probe when the trigger/handler are enabled (AuthError → exit 78).
  The rotation runbook step targets exactly these three forms of the same
  `slack_bot_token` key so doctor/probe and rotation agree.
- **Token-revocation blast radius (call out in runbook)**: Slack
  `invalid_auth`/`token_revoked` → `AuthError` → daemon halt (exit 78) per
  CONTRACTS. Consequence: a revoked/expired `dev_syssw_test` bot token takes down
  the **entire daemon** (pr_review + jira_triage included), not just ci_triage.
  This is the documented contract; the runbook must say so explicitly so on-call
  does not mistake a Slack-token expiry for a broader outage.
- **Redaction**: `infra/logging.py` already scrubs `xox*` + GitHub PAT + AWS +
  entropy; `strip_ansi` runs **before** redaction so ANSI-split tokens are still
  caught; full `--log-failed` text and the Claude prompt go to the redacted
  daemon local log only, never to Slack (FR-006, spec Q(Slack output)).
- **Read-only enforcement**: `gh` only `run view --log-failed` (with the
  dedicated unavailable-log classifier so a gone log skips, not retries forever);
  `oncall_wiki.py` exposes only `ensure_fresh()` (clone + `pull --ff-only`) +
  `search()` + `ls_remote()` (boot probe) — no commit/push/reset method exists,
  its two-layer path guard hard-bans `~/ssw-devops-oncall` regardless of
  `allow_external`, and every git op runs under the headless-safe env
  (`GIT_TERMINAL_PROMPT=0` + `BatchMode=yes` + managed 0600 known_hosts) so a
  first clone fails fast instead of hanging the daemon. Slack adapter's only
  write is `post_message`, channel-allowlist guarded. Enforced by code review
  against `contracts/` and by the guard unit tests.
- **Retention/backup**: `ci_triage_audit` rows cascade-prune with their `events`
  row (90-day default) — no new prune step. `slack_ci_alert_state` is 2 channel
  rows, never pruned. Both are in the SQLite DB, so both are included in
  `just backup` automatically (whole-DB snapshot). `app/prune.py` change is
  minimal/none. **`var/ssw-devops-oncall/` is on-disk, NOT in the DB — it is
  therefore NOT captured by the `just backup` SQLite snapshot.** That is fine
  because the vault is **fully rebuildable** (a fresh `ensure_fresh()` re-clones
  it; the corrupt-clone detector above will re-clone a damaged one). The runbook
  states this so backup scope is unambiguous and on-call knows `rm -rf
  var/ssw-devops-oncall/` is a safe recovery action. Vault disk growth is
  bounded in practice (small Obsidian vault, low MB) and `pull --ff-only`
  fast-forwards an existing clone; if it ever grows, deleting and re-cloning
  resets it.
- **Trigger-quiet visibility (on-call liveness)**: ci_triage is the channel
  on-call *expects* a first-pass in, so "the bot went quiet" must be
  distinguishable from "no alert worth triaging". A Slack read error makes the
  trigger log `slack_ci_alert.poll_failed` and, after the failure window,
  `TriggerSupervisor`/`PermanentFailureReporter` quarantines the poller
  **silently** (the `heartbeat.py` `tick_lag` self-alert does **not** cover a
  quarantined-but-alive trigger). The runbook therefore adds an explicit on-call
  step: **"if you stopped seeing CI Triage replies, run `inspect ci-triage` and
  check for a quarantine row"**, plus a low-cost liveness signal — the trigger
  emits a periodic structured `slack_ci_alert.poll_ok` log line (cycle count +
  per-channel cursor) that on-call's existing log alerting can watch for absence.
  This is an ops-runbook + cheap-log-line item, not a delivery-correctness change.
  **Active liveness signal (v1 doctor check; v2 heartbeat post).** Because
  log-absence alarming depends on on-call's external log pipeline actually watching
  for `poll_ok` absence (not built here), v1 also adds a concrete pull-based check:
  **`ops doctor` reports each `slack_ci_alert` channel's `last_seen_ts` age and a
  quarantine flag** (reading `slack_ci_alert_state` + the supervisor quarantine
  table), so an operator running `just doctor` sees "cursor for C09… last advanced
  18h ago / poller quarantined" without relying on log alerting. This is the
  promoted-to-a-real-signal step the review asked for, scoped to v1. A push-based
  daily "I am alive, N cursors at X" heartbeat *post* is deferred to **v2** and
  listed under §Risks — the doctor check is the v1 liveness floor and should land
  before `post_target=thread` promotion.
- **Silent-skip acknowledgement**: `skipped_log_unavailable` (GH log already
  expired/deleted) and `skipped_no_run_link` both `Ack` with **no Slack post** by
  default — the right default to avoid noise, but from the on-call seat "bot saw
  it but the log expired" is itself useful triage signal. The runbook states that
  **silence on an alert can mean `skipped_log_unavailable`, not "bot ignored a
  real failure"**, and that `[handlers.ci_triage].post_no_evidence_note=true`
  opts into a minimal "bot saw it; GH log expired / no machine-readable link;
  manual triage needed" note. **P3 default (not a suggestion)**: when
  `post_target` is flipped to `thread`, `post_no_evidence_note` is flipped to
  `true` in the same promotion step, so a skipped alert is acknowledged in-thread
  rather than disappearing (silence in the on-call channel is ambiguous). It stays
  `false` under `dry_run`.
- **Force-supersede leaves the prior post (no edit/delete)**: the force flow
  (spec Acceptance 1.5 / 2.5) posts a NEW `Updated triage (supersedes earlier
  comment at HH:MM:SS)` message and **leaves the prior one in place** — the Slack
  adapter is read-only-plus-`chat.postMessage` only (no `chat.update` /
  `chat.delete`), by design. Repeated force on a flapping run can stack several
  bot replies in one thread. Low severity (force is manual-only), but the runbook
  notes this so on-call is not surprised by multiple bot replies on one thread and
  knows the prior post is intentionally not deleted.
- **`owner_area` ↔ vault-domain re-pin (concrete trigger)**: the drift-guard test
  asserts the Pydantic `Literal` against a **pinned** list in
  `contracts/oncall-wiki-surface.md`, not the live vault, so a real vault domain
  change silently mis-routes attribution (undermining SC-005) until someone
  re-pins. The runbook lists a concrete re-pin trigger: **"re-pin the domain
  vocabulary whenever an incident file introduces a new `domain:` value, or on any
  oncall-vault schema change"** — so the pin does not silently rot.
- **Runbook**: add a `docs/RUNBOOK.md` section — how to seed/inspect channel
  cursors (`inspect ci-triage`), the **trigger-quiet → `inspect ci-triage` /
  check-quarantine** step above, how to flip `post_target` dry_run→thread
  (evidence-based, see P3), how to rotate the `slack_bot_token` (keychain
  `slack_bot_token` / file `slack_bot_token` / env `SLACK_BOT_TOKEN`), the
  **token-revocation halts the whole daemon** consequence, the **rare
  post-then-audit duplicate** crash artifact (and that once `post_target=thread`
  the duplicate lands in the real on-call channel, a slightly larger blast radius
  than feature 002's Jira comment), the **silent-skip can mean log-expired**
  signal, the **force-supersede leaves the prior post** behavior, the **re-pin
  domain vocabulary** trigger, and the "bot posts under CI Triage identity"
  expectation for on-call readers.

## Risks & Open Decisions

- **Bot-token-only posting (CLOSED)**: attribution token choice is closed — **bot
  only**. Consequence: triage cannot post as the operator. Mitigation (built in):
  `chat:write.customize` "CI Triage" identity + footer makes it visibly
  automated; **on-call ownership is preserved in the message body**
  (`owner_area`/`attribution`), not the poster account. No user-token code path
  is built.
- **R2 — dry_run → thread promotion**: open operational decision on *when* to
  flip `post_target` to `thread`. Plan ships the toggle in P1/P3 so promotion is
  a config change + runbook step, not a code change; requires operator
  confirmation after dry_run validation.
- **Slack-token revocation halts the WHOLE daemon (ACCEPTED TRADEOFF)**: a
  revoked/expired `dev_syssw_test` bot token → `AuthError` → exit 78 takes down
  the **entire daemon** (pr_review + jira_triage too), not just ci_triage. This
  is broader than the CONTRACTS `AuthError→halt` intent (designed for the *Claude
  OAuth* token); a third-party Slack token taking down two unrelated handlers is
  arguably too broad. **Accepted for v1** because (a) it matches the centralized
  exception→result contract — degrading only ci_triage would mean catching
  `AuthError` inside the handler, which CONTRACTS explicitly forbids; and (b) the
  boot probe is **gated on `ci_triage`/`slack_ci_alert` being enabled**, so only
  operators who turned the feature on are exposed. Revisit (per-feature auth
  degradation) only if it bites in practice. Documented here as an explicit
  tradeoff, and in the runbook for on-call.
- **P3 thread-reply promotion has no per-alert mute/opt-out (v2 candidate)**:
  once `post_target="thread"`, every candidate gets a reply; the only lever if
  the bot is noisy/wrong on a class of alerts is flipping the whole handler off.
  A future confidence-gated or per-source suppression (e.g. do not thread-reply
  when `attribution=unknown && confidence=low`) would protect channel trust.
  Deferred to v2; noted now so it is a conscious gap, not an oversight. **v1
  mitigation**: the P3 promotion is gated on a **mandatory evidence-based runbook
  review** of the `dry_run` audit rows' attribution/confidence distribution before
  the `post_target=thread` flip (see §Phased PR Slices P3), so the flip is not made
  while a non-trivial share of triages are `unknown`/`low`.
- **`owner_area` ↔ vault-domain drift is a maintenance touchpoint**: the
  drift-guard test asserts the Pydantic `Literal` matches a **pinned** domain
  vocabulary in `contracts/oncall-wiki-surface.md` (pinned for hermeticity, not
  read from the live vault). Consequence: a *real* vault drift silently mis-routes
  attribution (undermining SC-005) until someone re-pins. Acceptable for v1, with
  a **concrete re-pin trigger** in the runbook: re-pin whenever an incident file
  introduces a new `domain:` value, or on any oncall-vault schema change — so the
  pin does not silently rot.
- **R4 — daily/healthcheck coverage is partial**: `SSW-Alert-Bot` is Grafana
  alerting → Slack, so alert-rule-based healthcheck/daily failures *do* flow into
  `#alerts` via `attachments[].text` and are in v1 scope; broader daily-regression
  run coverage (not surfaced as an alert) remains a v2 extension via an additional
  trigger/alert source. Documented as a known partial.
- **Device-level evidence sufficiency**: some alerts (CP/SMC FW
  `0x50555746 device unreachable`) have no useful GitHub run log — the dual
  evidence path (host+window → Loki) covers these; risk is a missing/incorrect
  Loki window in the alert text ⇒ handler degrades to `confidence: low` rather
  than fabricating, per the prompt rule (SC-004).
- **Backlog thundering-herd (MITIGATED, no longer open)**: addressed by the
  first-class `max_per_cycle` cap + `staleness_seconds` re-seed wired into the
  state machine (CASE 1b / CASE 2) and `SlackCiAlertTriggerEntry` — not just a
  note. `conversations.history` cursor + tier-3 limits remain ample for 2
  channels at 120 s.
- **Silent skip on zero evidence**: an alert with neither a run link nor a Loki
  window currently `Ack`s + audits `skipped_no_run_link` with no Slack post.
  Defensible (no evidence = no noise), but `post_no_evidence_note` lets the
  operator opt into a minimal "bot saw it, manual triage needed" note so on-call
  knows the bot is alive on those alerts. **Default `false` under `dry_run`,
  flipped to `true` as part of the P3 `post_target=thread` promotion** (silence in
  the on-call channel is ambiguous) — see §Phased PR Slices P3 / §Rollout.
- **Trigger-quiet liveness — v1 doctor check, v2 push heartbeat (KNOWN GAP)**:
  ci_triage is the one channel where silence is ambiguous (no alert vs poller
  quarantined vs all-skipped). `heartbeat.py`'s `tick_lag` self-alert does **not**
  cover a quarantined-but-alive `slack_ci_alert` poller. **v1 mitigation (lands
  before thread promotion):** a periodic `slack_ci_alert.poll_ok` log line + an
  `ops doctor` cursor-age/quarantine liveness check + the runbook "stopped seeing
  CI Triage replies → `inspect ci-triage`/check quarantine" step. **v2:** a
  push-based daily "I am alive, N cursors at X" heartbeat *post* so liveness does
  not depend on on-call running a pull command or on external log-absence alarming.
  Deferred to v2; the v1 doctor check is the liveness floor.

**Verdict**: PASS. Phase 2 (`/speckit.tasks`) can proceed.
