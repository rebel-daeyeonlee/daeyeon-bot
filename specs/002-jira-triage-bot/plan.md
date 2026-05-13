# Implementation Plan: Jira Regression-Failure Triage Bot

**Branch**: `002-jira-triage-bot` | **Date**: 2026-05-13 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/002-jira-triage-bot/spec.md`

## Summary

When an SSWCI regression-failure ticket (titled `regression-test . <hostname>
. <TC-NNNN-...>`) is **assigned to daeyeon or to the DevOps Team**, the bot
polls and detects the assignment transition, walks up to the parent Epic to
get `branch + commit`, reproduces the ssw-bundle state at that commit in a
project-local clone, queries Loki for the run's log streams
(fwlog/smclog/kernel/syslog) and fetches RF artifacts via SSH from the test
host's log-dump directory, then assembles a Run Snapshot and calls Claude with
the operator's triage persona. The persona returns a structured TriageOutput
(Symptom / Evidence cited / Likely layer / Next data to collect, Korean prose +
English technical terms). The bot posts the result as a single Jira comment
via the REST API as the operator's identity.

The implementation extends the existing daemon by adding **one trigger**
(`jira_assigned`, 5-min polling) and **one handler** (`jira_triage`) plus a
small set of `infra/` adapters for Jira REST, Loki, SSH, and ssw-bundle git
operations. A new SQLite migration adds `jira_assigned_state` (per-issue
`in_pending_set` + `assignment_gen` — direct mirror of
`gh_review_requested_state` shipped in migration 002) and `jira_triage_audit`
(one row per posted/skipped/failed triage).
Auth uses three new secrets keys (`JIRA_USER`, `JIRA_API_TOKEN`,
`SSW_AUTOMATION_PASSWORD`) threaded through the existing Keychain/0600/env
provider chain — no new provider class. The existing `(source,
source_dedup_key)` UNIQUE on `events` carries the deterministic dedup token
`sha256("jira-assigned|{key}|{assignment_gen}")`.

## Technical Context

**Language/Version**: Python 3.12 (`requires-python = ">=3.12,<3.13"` in `pyproject.toml`).
**Primary Dependencies**: existing — `claude-agent-sdk`, `pydantic` (v2), `pydantic-settings`, `structlog`, `aiosqlite`, `typer`, `keyring`, `uuid-utils`. New runtime deps: **`httpx`** (Jira + Loki HTTP — widely transitive, basic-auth helper built-in) and **`asyncssh`** (SSH file access — async-friendly, MIT license, depends only on `cryptography` which is widely transitive). NOT adding the `jira` Python library — it's sync-only and `httpx` covers our needs directly with the same `(JIRA_USER, JIRA_API_TOKEN)` basic-auth shape that `ssw-bundle/inv/test_report/jira_client.py` already establishes. No MCP dependency on the hot path.
**Storage**: SQLite WAL (existing `state.db`). One additive migration: `005_jira_triage_state.sql` (002 already taken by GitHub PR-review state, 003/004 already shipped).
**Testing**: pytest (`pytest-asyncio` mode=auto), pytest-cov; integration tests use real `aiosqlite` against `tmp_path` DBs, real git operations against a fixture super-repo + submodule, fakes for Jira/Loki/SSH/Claude.
**Target Platform**: macOS (launchd) + Linux (systemd) — same artifact, same code paths. `git` and `ssh` CLIs must be on `PATH` (already true on all SSW dev machines).
**Project Type**: single-process daemon (existing `src/daeyeon_bot/`), not split.
**Performance Goals**: SC-001 (manual: 95% under 10 min) + SC-002 (auto: 95% under 15 min). Polling cadence 5 min ⇒ p50 detection ~2.5 min, p95 ~5 min. Per-event handler wall-clock budgeted at 600 s; dispatcher already polls outbox every ~200 ms.
**Constraints**: One triage = up to ~1 GB transient disk churn during ssw-bundle submodule checkout (mitigated by partial clone + reuse). Loki rate-limit is unbounded for one-operator traffic. Jira REST budget is 100 req/min per user (generous for ~1 ticket/5 min). SSH connection time ~1–2 s per host (negligible). Boot adds one Jira `GET /myself` probe (~200 ms) when triggers/handlers are enabled.
**Scale/Scope**: One operator. ~1–10 new SSWCI regression-failure tickets per day in steady state; long-tail spikes during release-branch instability. Concurrency=1 means events queue; budget-cap ensures no one event blocks the queue indefinitely.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The repo has no `.specify/memory/constitution.md`. The de-facto constitution
is the set of stable contracts in `CLAUDE.md`, `CONTRACTS.md`, and `docs/PLAN.md`.
This plan is gated against those:

| Gate | Source | Status | Notes |
|---|---|---|---|
| Single-tenant, single-process | CLAUDE.md "What this is" | PASS | No multi-tenancy, no broker, no API-key auth introduced. |
| Module layering one-way (`core ← infra ← triggers/handlers ← app ← cli`) | CLAUDE.md §Module layering | PASS | New trigger / handler stay in their layers; Jira/Loki/SSH ops via `infra/`. |
| One event = one transaction (no read-modify-write) | CLAUDE.md §One event, one cycle | PASS | Trigger UPSERTs `jira_new_issue_state` + writes `events`+`outbox` in one tx, per-PR. |
| Boot order fixed | CLAUDE.md §Boot order | PASS | Polling trigger registers via existing `app/registry.py:instantiate_trigger`; no boot-step reorder. Secrets-probe step 6 stays single-step (it already takes a list of keys). |
| At-least-once + idempotent handler | CONTRACTS.md §1 | PASS | `jira_triage` is `idempotent=True`; `side_effect_key=None` (Jira comment API has no idempotency key, but per-(issue_key, comment_seq) dedup happens BEFORE posting via `jira_triage_audit` lookup). |
| Outbox claim-row pattern | CONTRACTS.md §1 | PASS | Trigger uses `infra/outbox.py:insert_event` + `enqueue_handler`; handler returns `Ack`/`Retry`/`DeadLetter` only. |
| HandlerResult is the only exit | CONTRACTS.md §2 | PASS | Jira 401 → `AuthError` → daemon halt (exit 78); 429 with `Retry-After` → `RateLimitError` → `Retry`; 5xx/timeout → `TransientError`/`Retry`; missing persona / unresolvable ssw-bundle commit / submodule init failure → `DeadLetter` with explicit audit status. |
| Migration linear/additive | CLAUDE.md §Add a SQL column | PASS | One new file `005_jira_triage_state.sql`. No edits to `001`–`004`. |
| Secrets discipline | CLAUDE.md §Secrets discipline | PASS | Two new keys (`JIRA_API_TOKEN`, `SSW_AUTOMATION_PASSWORD`) thread through the same provider chain. New literal redaction pattern for the automation password lands in `infra/logging.py` BEFORE the handler ships. |
| Registry: explicit `if name == ...` | CLAUDE.md §Add new handler/trigger | PASS | `jira_triage` and `jira_new_issue` get explicit branches in `app/registry.py`. |
| No new runtime deps unless justified | CLAUDE.md feature-001 precedent | JUSTIFIED | `httpx` (Jira + Loki HTTP) and `asyncssh` (SSH log dump) are unavoidable. `httpx` is widely transitive in the Python async ecosystem; `asyncssh` is a single-purpose, stable lib. Both pinned in `pyproject.toml`. |

**Violations**: none. **Complexity Tracking** below is empty.

## Project Structure

### Documentation (this feature)

```text
specs/002-jira-triage-bot/
├── plan.md                          # this file
├── spec.md                          # /speckit.specify
├── research.md                      # Phase 0
├── data-model.md                    # Phase 1
├── quickstart.md                    # Phase 1
├── contracts/                       # Phase 1
│   ├── jira-rest-api-surface.md     # exact REST endpoints + JSON shapes used
│   ├── loki-query-surface.md        # LogQL queries + label conventions
│   ├── ssh-log-dump-surface.md      # SSH path layout + file kinds + size caps
│   ├── ssw-bundle-checkout-surface.md  # git ops + path guards
│   ├── claude-triage-output.md      # TriageOutput Pydantic schema + system prompt
│   └── persona-skill-format.md      # SKILL.md frontmatter+body contract (refs 001)
├── checklists/
│   └── requirements.md              # quality checklist
└── tasks.md                         # /speckit.tasks output
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
│   └── jira_triage/                         # NEW: domain types for this feature
│       ├── __init__.py                      # NEW
│       ├── types.py                         # NEW: TicketRef, RunMeta, RunSnapshot, EvidenceItem, TriageOutput dataclasses
│       └── persona.py                       # NEW: re-uses Persona dataclass from 001? Decision in research.md R6.
├── infra/                                   # adapters — depend on core
│   ├── outbox.py                            # (existing)
│   ├── storage.py                           # (existing)
│   ├── claude.py                            # (existing)
│   ├── secrets.py                           # MODIFIED: probe two new keys at boot
│   ├── logging.py                           # MODIFIED: add automation-password redaction pattern
│   ├── jira_client.py                       # NEW: httpx wrapper, REST v3 reads + REST v2 comment writes, basic auth
│   ├── jira_markup.py                       # NEW: Jira wiki-markup builders (heading, bullet, code, noformat, quote)
│   ├── loki.py                              # NEW: httpx wrapper around Loki query_range
│   ├── ssh_logs.py                          # NEW: asyncssh wrapper for log-dump fetch
│   ├── ssw_bundle.py                        # NEW: var/ssw-bundle/ clone manager (git ops + path guard)
│   ├── host_resolver.py                     # NEW: hostname→IP with in-process cache
│   ├── jira_triage_persona.py               # NEW: SKILL.md loader (or reuse 001's pr_review_persona if generalized)
│   ├── jira_triage_state.py                 # NEW: jira_assigned_state CRUD (mirror of pr_review_state.py)
│   ├── jira_triage_audit.py                 # NEW: jira_triage_audit CRUD
│   └── db/migrations/
│       ├── 001_init.sql                     # (existing) — DO NOT EDIT
│       ├── 002_gh_review_requested_state.sql  # (existing)
│       ├── 003_ratelimit_seed.sql           # (existing)
│       ├── 004_pr_review_audit_disallowed_repo.sql  # (existing)
│       └── 005_jira_triage_state.sql        # NEW
├── triggers/
│   ├── manual.py                            # (existing)
│   ├── gh_review_requested.py               # (existing)
│   └── jira_assigned.py                     # NEW: 5-min polling loop, per-issue state machine (mirrors gh_review_requested)
├── handlers/
│   ├── echo.py                              # (existing)
│   ├── pr_review.py                         # (existing)
│   ├── pr_review_schemas.py                 # (existing)
│   ├── pr_review_diff.py                    # (existing)
│   ├── jira_triage.py                       # NEW: load persona → resolve epic → checkout → collect logs → call Claude → post comment
│   └── jira_triage_schemas.py               # NEW: TriageOutput Pydantic v2 model
├── app/
│   ├── registry.py                          # MODIFIED: add `jira_triage` and `jira_new_issue` branches
│   ├── config.py                            # MODIFIED: add JiraConfig, LokiConfig, JiraAssignedTriggerEntry, JiraTriageHandlerEntry
│   ├── container.py                         # MODIFIED: instantiate JiraClient, LokiClient, SshLogClient, SswBundleClient at boot
│   └── ...                                  # (other existing files: untouched)
└── cli/
    ├── dev.py                               # MODIFIED: add `dev fire jira-triage --issue <key> [--force]`
    ├── inspect.py                           # MODIFIED: add `inspect jira-triage --issue <key>` to dump audit row
    └── ...                                  # (other existing files: untouched)

config.example.toml                          # MODIFIED: add [jira], [loki], [triggers.jira_new_issue], [handlers.jira_triage], routing entry
pyproject.toml                               # MODIFIED: add httpx, asyncssh
.gitignore                                   # MODIFIED: add var/

.claude/skills/daeyeon-bot-jira-triage/SKILL.md  # NEW: bundled default persona

var/                                         # NEW dir (gitignored)
└── ssw-bundle/                              # auto-managed by infra/ssw_bundle.py

tests/
├── unit/
│   ├── test_jira_client.py                  # NEW (httpx mock transport)
│   ├── test_loki.py                         # NEW (httpx mock transport)
│   ├── test_ssh_logs.py                     # NEW (asyncssh paramiko fake or local sshd fixture)
│   ├── test_ssw_bundle.py                   # NEW (fixture git repo under tmp_path)
│   ├── test_host_resolver.py                # NEW (monkeypatch socket.gethostbyname)
│   ├── test_jira_triage_persona.py          # NEW (mirrors 001's pr_review_persona test)
│   ├── test_jira_triage_state.py            # NEW (per-project cursor advance)
│   ├── test_jira_triage_audit.py            # NEW
│   ├── test_jira_triage_handler.py          # NEW (with all fakes + FakeClaudeSession)
│   ├── test_jira_assigned_trigger.py        # NEW (with FakeClock + FakeJira)
│   └── test_migration_005.py                # NEW
├── integration/
│   └── test_jira_triage_e2e.py              # NEW (real aiosqlite + real git fixtures + fakes for Jira/Loki/SSH/Claude)
└── fakes/
    ├── jira_client.py                       # NEW
    ├── loki.py                              # NEW
    ├── ssh_logs.py                          # NEW
    └── ssw_bundle.py                        # NEW (or shared git-fixture helper)
```

**Structure Decision**: extend the existing single-project layout. The new
feature lands as **one new trigger + one new handler + one additive migration
+ one config section + 5 new `infra/` adapters** plus a bundled persona skill
file and a gitignored `var/` runtime directory. This matches `CLAUDE.md` "Add
a new handler / Add a new trigger / Add a SQL column" recipes — extended once
for the multi-source-collection nature of this feature. No new top-level
package; no split into web app / mobile / etc.

**Implementation scope estimate**: spec FR-031's 600 s wall-clock budget is
the runtime per-event timeout, NOT a cap on this feature's source code. As a
self-imposed scope anchor: 1 trigger + 1 handler + 5 `infra/` adapters + 1
SQL migration + 1 config edit + 1 CLI edit + 1 registry edit + 1 persona
SKILL.md ≈ **15 source files + ~1500 lines** + ~10 test files. Anything
materially larger should prompt a re-scope discussion.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

(none)

## Post-design Constitution re-check

After Phase 1 (data-model + contracts), gates re-evaluated. No change from
the pre-Phase-0 verdict:

- New tables `jira_new_issue_state`, `jira_triage_audit` are additive in a
  single new migration `005_*.sql`. No edit to earlier migrations.
- Trigger writes go through `infra/outbox.py:insert_event` + `enqueue_handler`
  plus a single SQLite tx that also UPSERTs the per-project state row —
  no read-modify-write in app code.
- Handler returns only `Ack`/`Retry`/`DeadLetter`; Jira 401 ⇒ `AuthError` ⇒
  daemon halt (exit 78). Per-stage failures map to explicit `audit.status`
  enum values rather than to dispatcher results.
- Two new persistent secrets, but the provider chain doesn't gain a new
  class — the same factory adds them as named keys.
- The 5 endpoints in `contracts/jira-rest-api-surface.md` are the entire
  Jira write surface; FR-018 ("never modify ticket fields") is enforced by
  code review against that file.
- Banned-imports stays clean: `core/jira_triage/` is stdlib-only;
  `infra/jira_*.py`, `infra/loki.py`, `infra/ssh_logs.py`, `infra/ssw_bundle.py`,
  `infra/host_resolver.py` only import from `core/` and stdlib + their
  external libs (`httpx`, `asyncssh`).
- Wall-clock budget (`FR-031`) is enforced at the handler boundary via
  `asyncio.wait_for`, NOT by mutating dispatcher contracts. Adding a
  `timeout_s` field to `HandlerManifest` is out of scope for this feature;
  the handler self-enforces.

**Verdict**: PASS. Phase 2 (`/speckit.tasks`) can proceed.
