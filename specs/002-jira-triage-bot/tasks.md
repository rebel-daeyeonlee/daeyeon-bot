---
description: "Task list for Jira Regression-Failure Triage Bot (feature 002)"
---

# Tasks: Jira Regression-Failure Triage Bot

**Branch**: `002-jira-triage-bot`
**Input**: design documents in `/specs/002-jira-triage-bot/`
**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Included. The daemon project enforces strict pyright + pytest with coverage targets (`docs/PLAN.md` §6.3); the spec leans on the existing daemon's at-least-once / dead-letter / replay contracts that are already test-covered. Every new module gets unit tests, and the cross-cutting flow gets one integration test.

**Organization**: Phases run in order; tasks WITHIN a phase marked `[P]` can run in parallel (different files, no shared in-progress dependency). User stories from `spec.md` map to Phase 3 (US1/P1), Phase 4 (US2/P2), Phase 5 (US3/P2), Phase 6 (US4/P3).

## Format: `[ID] [P?] [Story?] Description with file path`

---

## Phase 1: Setup (shared infrastructure)

**Purpose**: Project-level wiring that every later phase depends on. None of these touch business logic — they prepare scaffolding.

- [ ] T001 Add the `[jira]`, `[loki]`, `[triggers.jira_assigned]`, `[handlers.jira_triage]`, retention extension (if any), and routing entries to `config.example.toml` exactly as specified in `data-model.md` §7. Set `enabled = false` for both the trigger and the handler. Do NOT enable in your local `config.toml` yet.
- [ ] T002 [P] Add `httpx` and `asyncssh` to `pyproject.toml`'s `[project] dependencies` with conservative version pins (`httpx >= 0.27, < 1.0`, `asyncssh >= 2.14, < 3.0`). Run `uv sync` and commit `uv.lock`.
- [ ] T003 [P] Add `var/` to `.gitignore` (repo root). Verify `git status` after `mkdir -p var/ssw-bundle` does NOT list `var/`.
- [ ] T004 [P] Create empty package `src/daeyeon_bot/core/jira_triage/__init__.py` (re-exports added in T011/T012/T013).
- [ ] T005 [P] Create migration file `src/daeyeon_bot/infra/db/migrations/005_jira_triage_state.sql` with the DDL from `data-model.md` §1 (two `CREATE TABLE IF NOT EXISTS`, three indices, the `UPDATE meta SET value='5'`). Verify `just migrate` applies it cleanly against a fresh `tmp_path` DB.
- [ ] T006 [P] Add `tests/unit/test_migration_005.py` asserting `meta.schema_version == '5'` and that both new tables (with all CHECK constraints) exist after `apply_pending_migrations`.

**Checkpoint**: Migration is idempotent and DB schema is at version 5. Dependencies in `pyproject.toml`. No source code beyond the SQL file and config-example block has changed.

---

## Phase 2: Foundational (blocking prerequisites for ALL user stories)

**Purpose**: Domain types, the secrets+redaction additions, the shared persona loader refactor, the five infra adapters, and config models. Every user story in Phase 3+ imports from these.

⚠️ **CRITICAL**: No user-story phase can start until this phase is complete.

### Secrets + redaction

- [ ] T007 In `src/daeyeon_bot/infra/secrets.py`: add `JIRA_USER`, `JIRA_API_TOKEN`, `SSW_AUTOMATION_PASSWORD` as named keys readable from the provider chain. The existing factory should already iterate names; this is just registering them. Add a `daeyeon-bot setup-token <name>` subcommand entry in `cli/lifecycle.py` (or wherever `setup-token` lives) for each.
- [ ] T008 In `src/daeyeon_bot/infra/logging.py:_REDACTION_PATTERNS`: add (a) a literal pattern that scrubs the in-memory `SSW_AUTOMATION_PASSWORD` value at boot time (look at how the OAuth token literal is added today — same mechanism), and (b) a regex pattern for Atlassian tokens (`ATATT[A-Za-z0-9_-]{40,}`). Document both additions in a code comment.
- [ ] T009 [P] Add `tests/unit/test_redaction_jira.py`: assert that (a) a log line containing the SSW automation password is redacted to `***`, (b) a log line containing an Atlassian token (`ATATT...`) is redacted. Use the same harness pattern as the existing redaction tests.

### Persona loader refactor

- [ ] T010 Rename `src/daeyeon_bot/infra/pr_review_persona.py` → `src/daeyeon_bot/infra/persona_loader.py`. Move the `Persona` dataclass from `src/daeyeon_bot/core/pr_review/persona.py` → `src/daeyeon_bot/core/persona.py`. In `core/pr_review/__init__.py` re-export `from daeyeon_bot.core.persona import Persona as Persona` for backwards compat. Update all imports in the existing pr_review code to use the new module path. The signature of `PersonaLoader.load(name, *, min_chars)` is unchanged; it now also accepts an optional `bundled_fallback_root: Path | None = None` arg for the repo-bundled fallback (used by `jira_triage`; defaults to None to preserve pr_review behavior). Update `tests/unit/test_pr_review_persona.py` import paths only — no behavior change.

### Core domain types (stdlib-only, no I/O)

- [ ] T011 [P] Create `src/daeyeon_bot/core/jira_triage/types.py` with the dataclasses from `data-model.md` §3: `TicketRef`, `TitleParse`, `EpicMeta`, `TimeWindow`, `SshDumpLocation`, `RunMeta`, `LokiSlice`, `SshArtifact`, `ProductCodeFile`, `RunSnapshot`, `EvidenceItem`, `SuspectedDuplicate`, `TriageDraft`, `PostedComment`. All `frozen=True, slots=True`. Stdlib only.
- [ ] T012 [P] Create `src/daeyeon_bot/core/jira_triage/audit.py` with the `AuditRow` dataclass per `data-model.md` §3.
- [ ] T013 [P] Update `src/daeyeon_bot/core/jira_triage/__init__.py` to re-export the public types from T011/T012.

### Config model extensions

- [ ] T014 In `src/daeyeon_bot/app/config.py`: add `JiraConfig` (base_url, timeout_seconds, issuetype_override), `LokiConfig` (base_url, timeout_seconds, per_stream_max_bytes, kernel_query_template, syslog_query_template), `JiraNewIssueTriggerEntry` (enabled, poll_interval_seconds=300, max_per_cycle=200), `JiraTriageHandlerEntry` (extends `HandlerEntry` with allowed_projects, persona_skill, min_persona_chars=200, timeout_seconds=600, ssw_bundle_path, allow_external_ssw_bundle=False, ssh_known_hosts_path, ssh_max_file_bytes, ssh_fetch_globs, branch_field_id, commit_field_id). Wire `Config.jira`, `Config.loki`, `Config.triggers.jira_assigned`, `Config.handlers.jira_triage` so the new TOML blocks parse. Keep existing fields untouched.
- [ ] T015 [P] Add `tests/unit/test_config.py` cases (extending the existing file) verifying: `[jira]` parses with defaults, `[loki].per_stream_max_bytes` default is 1 MB, `[triggers.jira_assigned].poll_interval_seconds=300` default, `[handlers.jira_triage].allowed_projects=["SSWCI"]` parses, `allow_external_ssw_bundle` defaults to False, env override `DAEYEON_BOT__HANDLERS__JIRA_TRIAGE__PERSONA_SKILL=other` works.

### Jira REST client

- [ ] T016 Create `src/daeyeon_bot/infra/jira_client.py` exposing an async `JiraClient` class with methods per `contracts/jira-rest-api-surface.md` §"Wrapper API": `myself()`, `discover_fields(project_keys)`, `search_jql(jql, fields, start_at, max_results)`, `issue_get(key, expand)`, `post_comment(key, body_wiki)`. Use `httpx.AsyncClient` with `httpx.BasicAuth(user, token)`. Error mapping: 401/403 → `AuthError`; 404 on GET issue → `PermanentError`; 400 on POST comment → `PermanentError`; 429 → `RateLimitError(retry_after)`; 5xx → `TransientError`. `post_comment` REJECTS non-string body via `TypeError`. `post_comment` uses `/rest/api/2/issue/{key}/comment`; reads use `/rest/api/3/...`.
- [ ] T017 [P] Create `tests/fakes/jira_client.py` with a `FakeJira` class implementing the same async interface backed by an in-memory dict of canned issues + a `posted_comments()` list for assertions.
- [ ] T018 [P] Add `tests/unit/test_jira_client.py` covering the wrapper's error mapping with `httpx.MockTransport`. At minimum: 401 → AuthError, 404 → PermanentError, 429 → RateLimitError, 5xx → TransientError, success path returns parsed JSON, `post_comment` accepts only str body, basic auth header is set.

### Jira wiki-markup builders

- [ ] T019 Create `src/daeyeon_bot/infra/jira_markup.py` with pure functions: `h3(title: str) -> str`, `bullet(text: str) -> str`, `code(text: str) -> str` (wraps in `{{...}}`), `noformat(text: str) -> str` (wraps in `{noformat}...{noformat}`), `quote(text: str) -> str` (wraps in `{quote}...{quote}`), `bold(text: str) -> str` (wraps in `*...*`). Plus `build_comment(triage: TriageDraft, *, supersede_header: str | None) -> str` that assembles the 4-section comment per `contracts/claude-triage-output.md` §4 + §8.
- [ ] T020 [P] Add `tests/unit/test_jira_markup.py` covering: h3 outputs `h3. ` prefix, code/noformat wrap correctly, build_comment includes all 4 sections in the right order, supersede header prepends a `{quote}...{quote}` block above the first heading, empty `evidence` list renders an empty bullet section (no exception), Korean text in `summary_md` passes through verbatim.

### Loki client + query builder

- [ ] T021 Create `src/daeyeon_bot/infra/loki.py` exposing `LokiClient.query_range(hostname, start, end, logql_filter=None, limit=5000, per_stream_max_bytes=...)` per `contracts/loki-query-surface.md` §"Wrapper API". Hostname is REQUIRED (TypeError if empty). `httpx.AsyncClient` (no auth). Error mapping: 4xx → empty slice + warn; 429 → backoff up to 3 attempts; 5xx → retry 3 then empty + audit. Plus `LokiQueryBuilder` static methods `fwlog_for`, `smclog_for`, `kernel_for`, `syslog_for` per the contract.
- [ ] T022 [P] Create `tests/fakes/loki.py` with `FakeLoki` returning canned `LokiSlice` per query key (hostname × stream).
- [ ] T023 [P] Add `tests/unit/test_loki.py` covering: hostname-empty raises TypeError, success path returns LokiSlice, byte-cap truncation marks `truncated=True`, 429 with Retry-After backs off, 5xx returns empty slice after retries.

### SSH log client

- [ ] T024 Create `src/daeyeon_bot/infra/ssh_logs.py` exposing `SshLogClient.fetch_directory(host, remote_path, globs)` per `contracts/ssh-log-dump-surface.md` §"Wrapper API". Use `asyncssh.connect` with `known_hosts=<state_dir>/jira_triage_known_hosts`, policy `accept-new`. Connection timeout = 10 s. SFTP listdir + per-file size check + per-file read up to `max_file_bytes`. Returns `SshFetchResult` with `artifacts`, `skipped`, optional `error`. Error mapping per the contract table.
- [ ] T025 [P] Create `tests/fakes/ssh_logs.py` with `FakeSshLogs` backed by a dict `{(host, remote_path): {filename: bytes}}` for in-memory replay.
- [ ] T026 [P] Add `tests/unit/test_ssh_logs.py` covering: successful fetch returns expected artifacts, oversized file gets skipped with `reason="oversized"`, listing non-existent path yields `error="path_not_found:..."`, auth fail yields `error="auth_failed"`, host-key change yields `error="host_key_changed:..."`. Use `asyncssh`'s test utilities or a tmp_path local sshd if available; otherwise skip with `pytest.mark.requires_local_sshd` and rely on integration coverage.

### ssw-bundle client

- [ ] T027 Create `src/daeyeon_bot/infra/ssw_bundle.py` exposing `SswBundleClient` per `contracts/ssw-bundle-checkout-surface.md` §"Wrapper API": `ensure_clone()`, `ensure_checkout(branch, commit_sha)`, `read_file(relative_path)`, `grep_test_case(tc_name)`. Constructor runs the path guards (reject path outside project root unless `allow_external=true`; ALWAYS reject `~/ssw-bundle/`; verify `.git/config`'s origin URL when `.git` exists). All git operations via `asyncio.create_subprocess_exec("git", ...)`. `ensure_checkout` runs fetch → checkout (detached) → submodule update --init --recursive --depth 1 under a single `asyncio.Lock`. Raises `UnresolvableCommitError` and `SubmoduleInitError` per the contract.
- [ ] T028 [P] Create `tests/fakes/ssw_bundle.py` (or shared helper) with a `tmp_path` git fixture mimicking ssw-bundle: super-repo with 2 commits + 1 fake submodule pointing at another tmp_path bare repo. The fixture is reused across all ssw-bundle tests.
- [ ] T029 [P] Add `tests/unit/test_ssw_bundle.py` covering: clone path outside project_root + `allow_external=false` → ConfigError; ~/ssw-bundle/ → ConfigError regardless of flag; ensure_clone() idempotent; ensure_checkout(unresolvable_sha) → UnresolvableCommitError; ensure_checkout(valid_sha) succeeds + submodule init; read_file inside clone works; read_file outside clone (via `..`) raises; grep_test_case finds a known TC name in the fixture.

### Host resolver

- [ ] T030 Create `src/daeyeon_bot/infra/host_resolver.py` exposing `HostResolver.resolve(name: str) -> str | None` (returns IP or None on DNS failure). Per-triage cache: instance is created per triage (or method-call cache via `functools.lru_cache` on the instance — pick whichever is cleaner).
- [ ] T031 [P] Add `tests/unit/test_host_resolver.py` covering: monkeypatched `socket.gethostbyname` returning an IP → cached on second call, raising `socket.gaierror` → resolve returns None, multiple distinct names cached independently.

### Audit + state CRUD

- [ ] T032 Create `src/daeyeon_bot/infra/jira_triage_audit.py` with async functions: `insert_audit(conn, **fields)`, `find_latest(conn, issue_key)`, `record_supersede(conn, audit_id, *, new_comment_id, new_posted_at)`. All operations single-statement.
- [ ] T033 [P] Add `tests/unit/test_jira_triage_audit.py` against `aiosqlite` in `tmp_path`. Cover: insert posted → find_latest returns it; insert each of the 7 CHECK enum statuses → all accepted; an unknown status → CHECK violation raised; supersede appends old `comment_id` and updates new one.
- [ ] T034 Create `src/daeyeon_bot/infra/jira_triage_state.py` with async functions: `get_state(conn, project) -> StateRow | None`, `seed_state(conn, project, initial_cursor)`, `upsert_cursor(conn, project, new_cursor)`.
- [ ] T035 [P] Add `tests/unit/test_jira_triage_state.py` covering: get_state returns None when missing, seed_state inserts a row, upsert_cursor advances forward but never backwards (regression check), concurrent transactions on different projects don't interfere.

**Checkpoint**: Foundation ready — Jira/Loki/SSH/ssw-bundle/host-resolver/persona/audit/state/config/redaction/migration all land. From this point each user story is implementable in isolation.

---

## Phase 3: User Story 1 — Manual triage of a specific Jira ticket (Priority: P1) 🎯 MVP

**Story goal**: Operator runs `daeyeon-bot dev fire jira-triage --issue SSWCI-NNNNN [--force] [--dry-run]` and within ~5–10 min a persona-driven, evidence-grounded triage comment appears on the ticket. The 7 skip statuses are exercised (title-miss, missing-metadata, unresolvable-commit, submodule-failure, already-triaged, persona-unavailable→DeadLetter, failed→DeadLetter). Force re-triage at the same ticket appends supersede header + preserves prior comment_id in audit history.

**Independent test (from spec.md)**: With a real test SSWCI ticket you have access to, run `dev fire jira-triage --issue ...`. Verify on Jira: one comment, four sections (Symptom / Evidence cited / Likely layer / Next data to collect), at least one evidence citation that matches a real log line in the run window. `daeyeon-bot inspect jira-triage --issue ...` shows audit row `status=posted`.

### Pydantic schema + parsing helpers

- [ ] T036 [P] [US1] Create `src/daeyeon_bot/handlers/jira_triage_schemas.py` with the Pydantic v2 `EvidenceItem`, `SuspectedDuplicate`, `TriageOutput` models from `contracts/claude-triage-output.md` §1, including `model_config={"extra": "forbid"}` and the `@model_validator` that enforces `evidence` non-empty when `domain != "unknown"`.
- [ ] T037 [P] [US1] Create `src/daeyeon_bot/handlers/jira_triage_parsing.py` with pure functions: `parse_title(summary: str) -> TitleParse | None` (regex per FR-008), `parse_timestamps(body_text: str) -> tuple[datetime, datetime] | None` (regex per FR-006), `parse_ssh_url(body_text: str) -> SshDumpLocation | None` (regex per FR-007), `extract_error_log(body_text: str) -> str` ({noformat} block extractor, defaults to first 4 KB if no block found). Stdlib only.
- [ ] T038 [P] [US1] Add `tests/unit/test_jira_triage_parsing.py`: parse_title accepts the canonical format + rejects misformatted titles; parse_timestamps handles microsecond precision + missing/malformed → None; parse_ssh_url extracts all four named groups; extract_error_log handles {noformat} blocks + raw description fallback.

### The handler

- [ ] T039 [US1] Create `src/daeyeon_bot/handlers/jira_triage.py` with `MANIFEST = HandlerManifest(name="jira_triage", idempotent=True, dedup_ttl=timedelta(days=1), side_effect_key=None, concurrency=1, accepts=("jira.assigned", "jira.triage.manual"))` and a `JiraTriageHandler` class. Constructor takes `(manifest, jira: JiraClient, loki: LokiClient, ssh: SshLogClient, ssw_bundle: SswBundleClient, host_resolver: HostResolver, persona_loader: PersonaLoader, claude_session_factory, audit_writer, config: JiraTriageHandlerEntry, jira_user: JiraIdentity, field_discovery: FieldDiscovery, project_root: Path)`. The `handle(event, ctx)` method runs the pipeline from `data-model.md` §4 wrapped in `asyncio.wait_for(timeout=config.timeout_seconds)`. Stages in order: (a) jira.issue_get → ticket body parsing → title regex; title miss → Ack + audit `skipped_not_regression_failure`; (b) load persona → ValidationError ⇒ DeadLetter; (c) parent Epic fetch → branch+commit; missing → Ack + audit `skipped_missing_metadata, missing_fields=[...]`; (d) audit history lookup for `issue_key`; if found + `event.payload.force is False` → Ack + audit `skipped_already_triaged`; (e) ssw_bundle.ensure_checkout(branch, commit); UnresolvableCommitError → Ack + audit `skipped_unresolvable_commit`; SubmoduleInitError → Ack + audit `skipped_submodule_failure`; (f) grep test_code; product_code excerpts gathered from a fixed set of "likely relevant" submodules based on the inferred domain (out-of-bounds: see "Open question" below); (g) host_resolver.resolve(hostname) → host_ip (or None); (h) parallel: loki.query_range × up to 4 + ssh.fetch_directory; per-channel failures populate audit.loki_error / ssh_error; (i) build Run Snapshot dict; (j) claude_session_factory().call(...) with persona as system_prompt + snapshot as user msg; parse JSON; validate `TriageOutput`; retry once on parse/validate failure; second failure → DeadLetter; (k) `_verify_evidence_quotes()` rejects fabricated quotes → PermanentError → DeadLetter; (l) `_redact()` rejects any pattern match → PermanentError → DeadLetter; (m) `infra/jira_markup.py:build_comment(...)` with supersede header if force=True over a prior posted row; (n) `jira.post_comment(issue_key, body_wiki=...)`; (o) record audit row `status='posted', comment_id, posted_at, domain, severity, summary_chars, evidence_count`; on force-supersede, also `record_supersede()` to update the prior row; (p) Ack. `asyncio.TimeoutError` → `TransientError` first time, `PermanentError` second time. Translate JiraClient/LokiClient/SshLogClient exceptions per their contract docs.
- [ ] T040 [US1] In `src/daeyeon_bot/app/registry.py:instantiate_handler` add `if name == "jira_triage":` branch that resolves all deps from the container (JiraClient, LokiClient, SshLogClient, SswBundleClient, HostResolver, PersonaLoader, claude_session_factory, audit writer, config, jira_user, field_discovery, project_root), and returns a `HandlerRecord` for `JiraTriageHandler`. Apply `_override_manifest` so config can override concurrency/accepts.
- [ ] T041 [US1] In `src/daeyeon_bot/app/container.py` (or wherever services are wired), construct one shared `JiraClient` (with credentials from secrets), one `LokiClient`, one `SshLogClient`, one `SswBundleClient` (with path-guard), one `HostResolver`, one `PersonaLoader` (with `bundled_fallback_root=<project_root>/.claude/skills/`). At boot, call `JiraClient.myself()` + `JiraClient.discover_fields(allowed_projects)` and cache `JiraIdentity` + `FieldDiscovery` for the daemon lifetime. Boot failure on any of these halts the daemon (AuthError → exit 78; ConfigError → exit 78). Add literal-value redaction for the `SSW_AUTOMATION_PASSWORD` value at boot before the handler ships (T008 wired the framework; this is where the literal value is captured).

### CLI entry points

- [ ] T042 [US1] In `src/daeyeon_bot/cli/dev.py` add a `dev fire jira-triage` Typer command: args `--issue <SSWCI-NNNN>`, `--force/-f`, `--dry-run`. The command builds an event with `type="jira.triage.manual"`, payload `{issue_key, force, comment_seq=("manual_<unix_ts>" if force else "1")}`, `source_dedup_key=sha256("manual-jira-triage|<key>|<seq>").hexdigest()`. With `--dry-run`, runs the full pipeline against the live Jira (read) + the real handler logic up to the point of `jira.post_comment` — instead prints the would-be comment to stdout. Otherwise calls `infra.outbox.insert_event` + `enqueue_handler` in one tx.
- [ ] T043 [US1] In `src/daeyeon_bot/cli/inspect.py` add an `inspect jira-triage` Typer command: args `--issue <key>` (also accept `--event-id <uuid>`). Queries `jira_triage_audit` for the matching row(s) and prints them in a readable table including status, domain, severity, comment_id, posted_at, summary_chars, evidence_count, loki_error, ssh_error, persona name, persona mtime_ns, missing_fields.

### Story-level tests

- [ ] T044 [P] [US1] Add `tests/unit/test_jira_triage_handler.py` using FakeJira + FakeLoki + FakeSshLogs + ssw_bundle_fixture + FakeClaudeSession + FakePersonaLoader + tmp_path SQLite. Cover ONE test each: (1) posts a comment with all 4 sections (happy path); (2) skipped_not_regression_failure when title doesn't match; (3) skipped_missing_metadata when Epic.branch is empty; (4) skipped_unresolvable_commit when SswBundleClient raises; (5) skipped_submodule_failure when SswBundleClient raises SubmoduleInitError; (6) skipped_already_triaged when audit history shows a prior posted row + force=False; (7) persona invalid → DeadLetter; (8) Claude returns malformed JSON twice → DeadLetter; (9) Claude returns fabricated quote not in snapshot → DeadLetter; (10) redaction match in `summary_md` → DeadLetter (no comment posted); (11) redaction match in `evidence.quote` → DeadLetter; (12) Loki unreachable → comment posts with `[loki: unavailable]` and audit `loki_error="..."`; (13) SSH unreachable → comment posts with `[ssh: ...]` and audit `ssh_error="..."`; (14) force=True with prior posted row → new comment posted with supersede header + audit row updated with `superseded_comment_ids` containing the old id; (15) wall-clock timeout fires `TransientError` first then `PermanentError` second time.
- [ ] T045 [P] [US1] Add `tests/integration/test_jira_triage_e2e.py` mounting real `aiosqlite` against `tmp_path`, real `apply_pending_migrations` (including 005), real outbox + dispatcher, real `ssw_bundle_fixture` (tmp_path git super-repo + submodule), and FakeJira + FakeLoki + FakeSshLogs + FakeClaudeSession. Drive end-to-end: `cli dev fire jira-triage --issue SSWCI-X` → outbox row written → dispatcher claims → handler runs all stages (including REAL git ops against the fixture) → FakeJira.post_comment records the call → audit row `status='posted'`. Mark `@pytest.mark.integration`.

**Checkpoint**: User Story 1 is independently shippable. Manual triage works end-to-end. All 7 skip statuses, all 3 dead-letter paths, force-supersede, and the timeout ladder are covered. Auto-trigger (Story 2) NOT yet wired.

---

## Phase 4: User Story 2 — Auto triage on new SSWCI regression-failure tickets (Priority: P2)

**Story goal**: Polling trigger detects new SSWCI tickets matching the title regex and emits `jira.new_issue` events into the outbox. The same handler from US1 processes them. Cursor advances atomically; replays are no-ops.

**Independent test (from spec.md)**: File a new SSWCI regression-failure ticket. Within ~10 minutes a triage comment appears without any operator command.

### The trigger

- [ ] T046 [US2] Create `src/daeyeon_bot/triggers/jira_assigned.py` with `MANIFEST = TriggerManifest(name="jira_assigned", source="jira_assigned", retryable_at_source=False)` and a `JiraAssignedTrigger` class. Constructor takes `(jira: JiraClient, storage_factory, allowed_projects, team_name, team_field_id, poll_interval_seconds, max_per_cycle, clock, pause_check)`. The trigger's main loop (mirrors `gh_review_requested`):
   - Check `meta.jira_assigned_state_seeded` flag.
   - Build JQL: `(assignee = currentUser() OR "Team" = "{team_name}") AND project IN ({allowed}) AND summary ~ "regression-test" AND status != Closed` (omit the `OR "Team" = ...` clause if `team_name` is empty).
   - Paginate `jira.search_jql(...)` until fewer than `maxResults` or `max_per_cycle` cap.
   - Build `page_now` set: for each returned ticket, determine `assignee_path ∈ {"user","team"}` by re-reading `assignee.accountId == myself.accountId` (→ "user") vs. team field match (→ "team"). If a ticket matches both, classify as "user".
   - Build `page_prev` set: `SELECT issue_key FROM jira_assigned_state WHERE in_pending_set=1`.
   - For each issue in `page_now ∪ page_prev`, apply the state machine from `data-model.md` §5 (cases 1–5) inside a single aiosqlite tx; events.insert + outbox.enqueue happen only on CASE 1 and CASE 2.
   - If `meta.jira_assigned_state_seeded != '1'`: seed-only pass — INSERT every observed issue with `in_pending_set=1, assignment_gen=1, last_observed_at=now`, no events. After: `UPDATE meta SET value='1' WHERE key='jira_assigned_state_seeded'`. Exit cycle.
   - Skip ticket entirely if title doesn't match the regression-test regex (FR-004) — that ticket is not in `page_now` for state-machine purposes (treat as if JQL excluded it). The bot is conservative: state row IS still maintained (so if title changes later and re-enters, the trigger picks it up), but emission is skipped.
   - Error map: `AuthError` → halt (raise); `RateLimitError` → sleep `Retry-After` then continue; `TransientError`/`PermanentError` → log + continue (next cycle).
- [ ] T047 [US2] In `src/daeyeon_bot/app/registry.py:instantiate_trigger` add `if name == "jira_assigned":` branch that resolves the JiraClient + state factory + config and returns a `TriggerRecord` for `JiraNewIssueTrigger`.

### Trigger tests

- [ ] T048 [P] [US2] Add `tests/unit/test_jira_assigned_trigger.py` with FakeJira + FakeClock + tmp_path SQLite. Cover: (1) cold-start with empty state: 3 tickets observed → 3 state rows seeded with `in_pending_set=1`, **zero events emitted**, `meta.jira_assigned_state_seeded='1'`; (2) post-seed, a ticket newly enters the set → CASE 1 fires, event emitted with gen=1, audit-side dedup check passes; (3) ticket re-enters after leaving (in_pending_set goes 1→0 via a prior poll without it, then 0→1 next poll) → CASE 2, gen=2, event emitted; (4) ticket stays in the set across two polls → CASE 3, last_observed_at updates, no event; (5) ticket leaves the set → CASE 4, in_pending_set=0, no event; (6) overlapping polls observe the same `(issue_key, gen)` twice → only one event row (events.UNIQUE no-op); (7) ticket whose title doesn't match the regex → state row maintained but emission skipped; (8) pagination: 50 + 30 in two pages → trigger fetches both, processes all in one cycle; (9) `max_per_cycle=10` caps; (10) AuthError halts; (11) RateLimitError with Retry-After sleeps; (12) team-only match (assignee != me but `Team`="DevOps") → audit `assignee_path="team"`.

### Integration test for auto path

- [ ] T049 [P] [US2] Extend `tests/integration/test_jira_triage_e2e.py` with a second test (`@pytest.mark.integration`) that drives the AUTO path: register the `jira_assigned` trigger against `FakeJira` populated with one matching ticket → run one poll cycle → assert the outbox row was written → dispatcher claims → handler runs → audit row `status='posted'`. Same `ssw_bundle_fixture` reused.

**Checkpoint**: User Story 2 is independently shippable. Polling + dedup + cursor advancement all verified.

---

## Phase 5: User Story 3 — Persona governs triage and integrates ssw-debugger principles (Priority: P2)

**Story goal**: Repo-bundled persona ships with the spec; operator can override locally; mtime hot-reload picks up edits without restart.

**Independent test (from spec.md)**: Run a manual triage. Edit the persona to add a new rule. Run another triage. Verify the new rule's effect.

### Bundled persona

- [ ] T050 [US3] Author the bundled persona at `<project_root>/.claude/skills/daeyeon-bot-jira-triage/SKILL.md` per `contracts/persona-skill-format.md` §5 + §6. Include: Role (one para), Operating principles (5 rules), Context shape (Run Snapshot field-by-field), Domain classification (6-ENUM table cribbed from oh-my-debugger:short-triage with attribution), Output contract (prose restatement of the JSON schema with examples), Hard rules (anti-patterns), Language (Korean prose + English technical terms), Stage 1 / Stage 2 split per `research.md` R16. ≥ 200 chars after frontmatter strip (probably 2-4 KB total).
- [ ] T051 [P] [US3] Add `tests/unit/test_persona_bundled.py` (CI lint): assert `<project_root>/.claude/skills/daeyeon-bot-jira-triage/SKILL.md` parses (frontmatter + non-empty body), body ≥ 200 chars after frontmatter strip, body contains the keywords "Symptom", "Evidence", "Likely layer", "Next data to collect" (case-insensitive substring) so a future drift catches itself in CI.

### Hot-reload integration test

- [ ] T052 [US3] Add `tests/integration/test_jira_triage_persona_reload.py` (`@pytest.mark.integration`): drive two triages back-to-back against `FakeJira` + `FakeClaudeSession`. Between the two triages, `os.utime()` the bundled persona file to bump its mtime. Capture the system_prompt the Fake session received on each call; assert the second one was re-read from disk (mtime_ns differs). Persona body content can be a sentinel like "PERSONA_V1" / "PERSONA_V2" written between calls.

**Checkpoint**: User Story 3 is independently shippable. Persona authoring + hot-reload + bundled-fallback all verified.

---

## Phase 6: User Story 4 — Operator pause kill-switch (Priority: P3)

**Story goal**: While paused, the handler holds events without posting to Jira; on resume, it processes queued events normally.

**Independent test (from spec.md)**: Pause the bot, enqueue several events, resume, observe each posts exactly once.

- [ ] T053 [US4] Verify (no code change should be required — the existing PAUSE kill-switch in `app/pause.py` already short-circuits handler calls). Add `tests/integration/test_jira_triage_pause.py` (`@pytest.mark.integration`): bot running with the handler enabled → `touch ~/.daeyeon-bot/PAUSE` → enqueue 3 jira.new_issue events → assert 0 FakeJira.post_comment calls → remove PAUSE → assert all 3 post within reasonable wall-clock + each posts exactly once + audit rows look right.

**Checkpoint**: User Story 4 is independently shippable. Pause kill-switch interop verified.

---

## Phase 7: Operator-facing docs

**Purpose**: Make the feature discoverable + operable by someone other than the implementor.

- [ ] T054 [P] In `docs/RUNBOOK.md` add an incident playbook for `JIRA_API_TOKEN` expiry — mirrors the `gh auth` playbook from feature 001's RUNBOOK update. Include the symptom (daemon exits 78 with `AuthError`), the diagnosis command (`uv run daeyeon-bot ops doctor`), and the fix (`uv run daeyeon-bot setup-token jira-api-token`).
- [ ] T055 [P] In `docs/RUNBOOK.md` add a section "SSH key migration plan for SSW_AUTOMATION_PASSWORD" capturing the FR-021 follow-up: generate `~/.daeyeon-bot/ssh/id_ed25519`, distribute pubkey to test hosts under `automation`'s `authorized_keys`, flip `[handlers.jira_triage]` to prefer key auth. Mark as "follow-up, not v1".
- [ ] T056 [P] In `README.md` add a row to the "Built-in triggers/handlers" table for `jira_assigned` / `jira_triage` alongside the existing `gh_review_requested` / `pr_review` row. Link to `specs/002-jira-triage-bot/quickstart.md`.

**Checkpoint**: Documentation reflects the new feature; an operator who didn't write the code can enable it from the runbook.

---

## Phase 8 (deferred, separate spec): Skill-tool delegation

Per `research.md` R16, this is **not** part of this feature's tasks.
A future spec extension will:

- Extend `infra/claude.py` to accept `allowed_tools`/`mcp_servers` from `[handlers.jira_triage]`.
- Add `[handlers.jira_triage].enable_skill_tool` config knob (default false).
- Add an integration test where the bot's Claude session invokes `/oh-my-debugger:short-triage` and incorporates the result.

The persona's Stage 2 block is already present, so this is purely a SDK-options change. No persona edit required.

---

## Open questions (resolve during implementation review, not blocking)

1. **Product-code excerpt selection**: T039 stage (f) gathers "likely relevant submodule files". For v1, the simplest cut is to NOT pre-select — pass empty `product_code` and let the persona decide what additional files (citations) it wants the bot to surface in a follow-up. Alternative: at T039 stage (f), grep the error log + dmesg for stack-trace file paths, glob those under the matching submodule, and include the matched files. The more sophisticated cut likely lands in PR-2-followup based on how well empty-product_code triages perform.
2. **`output.xml` parsing**: T024's SSH fetch retrieves the file whole. For v1, the bot sends it to Claude as `ssh.output_xml` raw text (and the file is typically ~1 MB → ~6 KB after `\n` collapse). A future task could pre-parse the FAIL message + tags via a tiny robot-output XML reader, reducing token usage. Out of scope for this spec.

These are flagged in plan.md "Open questions" too; the implementor is expected to make a call and document it in the PR description.

---

## Task ID summary

- Phase 1 (Setup): T001–T006 (6 tasks)
- Phase 2 (Foundational): T007–T035 (29 tasks)
- Phase 3 (US1 — manual triage): T036–T045 (10 tasks)
- Phase 4 (US2 — auto triage): T046–T049 (4 tasks)
- Phase 5 (US3 — persona): T050–T052 (3 tasks)
- Phase 6 (US4 — pause): T053 (1 task)
- Phase 7 (Docs): T054–T056 (3 tasks)

**Total**: 56 tasks. Approximate landing: PR-1 = T001–T035 (no daemon-behavior change); PR-2 = T036–T053; PR-3 = T054–T056.
