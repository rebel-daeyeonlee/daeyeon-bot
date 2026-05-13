# Specification Quality Checklist: Jira Regression-Failure Triage Bot

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-05-13
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs) — implementation choices live in `plan.md` / `research.md` / `contracts/`.
- [x] Focused on user value and business needs — every FR ties back to "daeyeon walks into a comment-with-evidence" instead of a blank ticket.
- [x] Written for non-technical stakeholders — section structure and Korean clarification Q/A pairs work for the operator without coding-language assumptions.
- [x] All mandatory sections completed (Clarifications, User Scenarios & Testing, Edge Cases, Requirements, Success Criteria, Assumptions).

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain — every open question resolved in conversation 2026-05-13 + recorded in Clarifications.
- [x] Requirements are testable and unambiguous — every FR cites an enforcement point or audit field.
- [x] Success criteria are measurable — SC-001..SC-012 all have either a numeric threshold, a 100% check, or an audited percent.
- [x] Success criteria are technology-agnostic — `httpx` / `asyncssh` / `aiosqlite` do not appear in SC text.
- [x] All acceptance scenarios are defined — 4 user stories, each with 3–6 acceptance scenarios.
- [x] Edge cases are identified — 16 entries covering title-regex miss, missing Epic fields, ssw-bundle resolution failures, network outages, force re-triage, secrets-in-evidence, etc.
- [x] Scope is clearly bounded — Out-of-scope list explicit in Assumptions.
- [x] Dependencies and assumptions identified — secrets keys, host DNS, Loki reachability, ssw-bundle remote, oh-my-debugger plugin install.

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria — FR-001..FR-031 map to SC-001..SC-012 + audit table CHECK enum.
- [x] User scenarios cover primary flows — US1 (manual triage, P1), US2 (auto triage, P2), US3 (persona, P2), US4 (pause, P3).
- [x] Feature meets measurable outcomes defined in Success Criteria — gates on SC-001 (≤10 min P1), SC-002 (≤15 min P2), SC-006 (zero duplicates), SC-008 (100% redaction), SC-011 (no working-tree mutation), SC-012 (Korean output).
- [x] No implementation details leak into specification — the only library names that appear in `spec.md` are in Clarifications and Assumptions (where they're behavioral contracts, not implementation prescriptions).

## Pre-merge gates (for the implementing PR series)

These are NOT spec-quality items; they're carry-forward checks the
implementor MUST satisfy before each PR in this feature merges. Listed
here so the reviewer doesn't have to re-derive them from `plan.md` /
`research.md`.

### PR-1 (infrastructure, no daemon behavior change)

- [ ] Migration `005_jira_triage_state.sql` applies cleanly via `just migrate`; `meta.schema_version` advances to 5.
- [ ] `pyproject.toml` adds `httpx` and `asyncssh` with sensible version pins; `uv sync` succeeds; `uv.lock` updated.
- [ ] `var/` added to `.gitignore`. `git status` after a fresh `just sync` does NOT list `var/`.
- [ ] Redaction patterns extended in `infra/logging.py`: literal value of `SSW_AUTOMATION_PASSWORD` from secrets (at boot), Atlassian token regex `ATATT[A-Za-z0-9_-]{40,}`. Unit test asserts both patterns scrub correctly.
- [ ] Each new `infra/` module has a unit test against fakes / mock transport: `test_jira_client.py`, `test_jira_markup.py`, `test_loki.py`, `test_ssh_logs.py`, `test_ssw_bundle.py`, `test_host_resolver.py`, `test_persona_loader.py`.
- [ ] `infra/ssw_bundle.py` path guards reject the operator's working tree (`~/ssw-bundle/`) — covered by a dedicated unit test.
- [ ] `infra/persona_loader.py` refactor preserves the old `infra/pr_review_persona.py` symbol via re-export. `tests/unit/test_pr_review_persona.py` still passes unchanged.
- [ ] `just check` (lint + typecheck + tests) is green.

### PR-2 (trigger + handler + persona)

- [ ] `triggers/jira_new_issue.py` registered in `app/registry.py:instantiate_trigger`.
- [ ] `handlers/jira_triage.py` registered in `app/registry.py:instantiate_handler`.
- [ ] `[triggers.jira_new_issue]` and `[handlers.jira_triage]` blocks added to `config.example.toml` with `enabled = false` defaults.
- [ ] Routing entries added: `"jira.new_issue" = ["jira_triage"]`, `"jira.triage.manual" = ["jira_triage"]`.
- [ ] Bundled persona shipped at `.claude/skills/daeyeon-bot-jira-triage/SKILL.md`; CI lint asserts it parses and exceeds `min_persona_chars`.
- [ ] Handler wraps `handle()` in `asyncio.wait_for(timeout=config.timeout_seconds)`.
- [ ] Audit row written for every outcome path (the 7 CHECK enum values are all reachable from at least one test).
- [ ] Strict redaction: any pattern match in `summary_md` or `evidence.quote` raises `PermanentError → DeadLetter` (no silent rewrite). Verified by unit tests on both `summary_md` and `evidence[*].quote`.
- [ ] `_verify_evidence_quotes()` rejects fabricated quotes (covered by a test where Claude returns a quote not present in the snapshot).
- [ ] `dev fire jira-triage --issue <key> [--force] [--dry-run]` CLI command works end to end against `FakeJira`/`FakeLoki`/`FakeSshLogs`/`FakeClaudeSession`.
- [ ] `inspect jira-triage --issue <key>` prints the audit row legibly.
- [ ] Integration test mounting real `aiosqlite` + real git fixture exercises the full pipeline.
- [ ] `just check` is green.

### PR-3 (operator-facing docs)

- [ ] `docs/RUNBOOK.md` gains an incident playbook for `JIRA_API_TOKEN` expiry (mirrors the `gh auth` playbook from feature 001).
- [ ] `docs/RUNBOOK.md` notes the long-term plan to switch SSH auth from password to key (FR-021 follow-up).
- [ ] `README.md` lists the new feature under "Built-in triggers/handlers" alongside `gh_review_requested` / `pr_review`.
- [ ] `quickstart.md` (in this spec dir) cross-linked from `README.md`.

### PR-4 (deferred — Skill-tool delegation, separate spec extension)

Not part of this feature. Tracked as a follow-up:
- [ ] `infra/claude.py` extended to accept `allowed_tools` / `mcp_servers` from `[handlers.jira_triage]`.
- [ ] `[handlers.jira_triage].enable_skill_tool` config knob added (default `false`).
- [ ] Integration test: with the knob on, the bot's Claude session can invoke `/oh-my-debugger:short-triage`.
- [ ] Persona body's Stage 2 block becomes active (no edit required — already present).

## Notes

- Items in **Pre-merge gates** are an operator promise for the implementing PRs, not part of the spec quality bar. They're enumerated here so the reviewer of each PR can mark them off the same document.
- The spec deliberately does **not** restate the daemon's at-least-once / dead-letter / pause / lifecycle guarantees as new requirements; they are referenced in the Assumptions section so `/speckit.plan` can map FRs to existing infrastructure. Same posture as feature 001.
- Informed defaults used in lieu of `[NEEDS CLARIFICATION]` markers: comment-author identity (operator's account), team-level Jira filters excluded from v1, draft-PR-equivalent handling (we never look at draft tickets — there's no such concept), triage focus (defined by the persona, not hard-coded), persona file location/format (matches feature 001 pattern). Good candidates to revisit if the operator disagrees during pre-merge review.
