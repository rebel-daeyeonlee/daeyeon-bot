# Feature Specification: Jira Regression-Failure Triage Bot

**Feature Branch**: `002-jira-triage-bot`
**Created**: 2026-05-13
**Status**: Draft
**Input**: User description: "Jira bot 기능 추가 — 나에게 할당됐거나 DevOps 팀에 할당된 SSWCI regression failure 티켓이 보이면 자동으로 트리아지 코멘트를 단다. 코멘트는 ssw-debugger(=oh-my-debugger 플러그인) 스킬들이 인코딩하고 있는 운영자의 NPU 디버깅 원칙(증거 우선, 레이어 귀속 우선, evidence-grounded synthesis)을 따른다. 트리거는 assignee/Team 기반 폴링, 액션은 코멘트 추가 한 가지(read-mostly). 본문에 박혀 있는 Epic→branch+commit, Start/End timestamps, SSH 로그 덤프 URL을 종합해서 Loki + SSH dump + ssw-bundle 소스를 모두 보고 root-cause hypothesis를 단다."

## Clarifications

### Session 2026-05-13

- Q: Jira 인증 방식 → A: Jira REST API 직접 호출(httpx) + **basic auth `(JIRA_USER, JIRA_API_TOKEN)`**. ssw-bundle의 `inv/test_report/jira_client.py`가 이미 같은 두 env var를 쓰는 컨벤션이라 동일 키로 통일 — 운영자가 토큰 한 개만 관리. 두 키 모두 daemon의 기존 secrets provider chain(keychain → 0600 file → env w/ `--insecure-env`)으로 주입. Atlassian MCP 서버 경유 금지(MCP 의존을 핫패스에 두지 않는다). Jira host는 `https://rbln.atlassian.net/` 고정 — config knob 하나(`jira.base_url`)로 노출.
- Q: 트리거 단위와 JQL → A: **나에게 할당됐거나 DevOps Team에 할당된 SSWCI regression failure 티켓**. JQL: `(assignee = currentUser() OR "Team" = "DevOps") AND project IN (allowed) AND summary ~ "regression-test" AND status != Closed`. "Team"은 Jira Atlassian Teams 필드 (Branch/Commit과 같이 boot-time discovery로 field ID 확보). 상태는 `gh_review_requested` 패턴 미러링 — `jira_assigned_state(issue_key, project, in_pending_set, assignment_gen, last_observed_at)` 한 행/issue. 발사 조건: (a) 처음 set 진입 — gen=1 emit; (b) set 떠났다 재진입 — gen += 1 emit; (c) 같은 set 안에 머무름 — emit 없음 (UNIQUE 노옵). Dedup_token = `sha256("jira-assigned|{key}|{gen}")`. **Cold-start**: 첫 부팅 시 관측되는 issue는 시드만 (in_pending_set=1, gen=1) — emit 없음. 기존 큐의 retroactive triage 금지.
- Q: 액션 범위 → A: **이슈 코멘트 추가 한 가지**. Label/priority/assignee/status transition은 v1에서 절대 손대지 않음 (pr_review가 `event=COMMENT` 외 다른 review event를 절대 안 쓰는 것과 같은 정책). 멱등성 보장은 `jira_triage_audit` 행 lookup으로 — 동일 `(issue_key, comment_seq)`에 대해 중복 POST 방지. 코멘트 body는 **Jira wiki markup** (`h3.`, `*bold*`, `{noformat}` 등) — ssw-bundle의 `inv/test_report/jira_markup.py`가 이미 쓰는 마크업과 동일. ADF가 아닌 이유는 wiki markup이 더 간결하고 팀 코드와 일관되기 때문. POST는 REST v2 `/rest/api/2/issue/{key}/comment` (v2는 wiki markup body 수락), search/issue-get은 REST v3 사용.
- Q: 페르소나 위치/포맷/변종 선택 → A: pr_review와 동일 패턴. `~/.claude/skills/<name>/SKILL.md` (Claude Code skill 포맷; frontmatter 무시, body만 system prompt). 본 레포에 `daeyeon-bot/.claude/skills/daeyeon-bot-jira-triage/SKILL.md`를 번들 기본본으로 제공하고 사용자 홈이 override. `[handlers.jira_triage].persona_skill = "daeyeon-bot-jira-triage"`로 활성 변종 선택. 매 트리아지마다 mtime stat → 변경 시 재읽기.
- Q: 페르소나의 oh-my-debugger 결합 방식 → A: **Option C (하이브리드)**. SKILL.md는 skill-위임 스타일로 작성하되 PR-2 시점에는 system prompt만으로 동작 (핸들러가 미리 만든 context를 분석). SDK options(`allowed_tools` 등) 확장으로 Skill tool을 실제 호출 가능하게 만드는 작업은 별도 PR(PR-4)로 분리. SKILL.md 본문에 "Stage 1: handler가 준 context만 분석 / Stage 2 (PR-4): /oh-my-debugger:short-triage 호출 가능"을 명시.
- Q: 트리아지 출력 언어 → A: 한국어 산문 + 영어 기술어/경로/로그 라인 원문. `oh-my-debugger:triage`가 정확히 이 스타일. pr_review의 "영어 default" 결정과는 다름 — Jira 코멘트는 SSW 팀 내부 소통이라서.
- Q: ssw-bundle 클론 위치 → A: 프로젝트 루트 아래 `daeyeon-bot/var/ssw-bundle/`. gitignored. 사용자의 working tree `~/ssw-bundle/`은 **절대 건드리지 않음**. `infra/ssw_bundle.py`가 경로 검증 가드를 둠. `git clone --filter=blob:none`로 초기 partial clone, 이후엔 `git fetch + git checkout + git submodule update --init --recursive`만.
- Q: 데이터 수집 채널 → A: 세 채널 모두 사용. (1) **Loki** (`http://loki.ssw.rbln.in`, no auth) — fwlog/smclog/kernel/syslog 스트림. fwlog/smclog는 `test_name` 라벨로 좁히고, 모든 쿼리에 hostname 라벨 필수. (2) **SSH 로그 덤프** (`ssh://automation@<host>:/mnt/data/logs/regression-test/<run-id>/<host>/<TC>/`) — RF output.xml, log.html, 캡처된 raw dmesg, core dump. `automation:automation` shared 자격증명, `SSW_AUTOMATION_PASSWORD` 키로 secrets provider 경유. (3) **ssw-bundle 소스 트리** — Epic의 branch+commit로 checkout 후 test 코드와 product 코드(submodule) 읽기.
- Q: 티켓 파싱 키 → A: 제목은 고정 포맷 `regression-test . <hostname> . <TC-NNNN-...>` — 단일 정규식으로 hostname + tc 추출. Start/End timestamps + SSH dump URL은 본문에서 별도 정규식. Epic branch+commit은 parent Epic 이슈를 한 번 더 fetch(`expand=names,renderedFields`)해서 추출. 어느 하나라도 누락 시 진단 대신 audit `skipped_missing_metadata`로 기록.
- Q: hostname↔IP 변환 → A: 사내 DNS가 `ssw-giga-02` 형태 호스트네임을 해석함(확인됨). `socket.gethostbyname` 한 줄로 해결. fwlog/smclog Loki 쿼리 시 IP 라벨 필요한 곳만 변환, kernel/syslog는 이름 그대로. 트리아지 1회 동안 in-process 캐시.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Manual triage of a specific Jira ticket (Priority: P1)

The operator points the bot at a single Jira regression-failure ticket (by issue key, e.g. `SSWCI-16787`) and the bot produces a triage comment based on the operator's persona. The posted comment includes a structured Symptom / Evidence cited / Likely layer / Next data to collect block, with every Evidence bullet citing a concrete source (Loki line, dmesg line, source `file:line`).

**Why this priority**: Without this slice nothing else is meaningful. It validates the full pipeline (Jira fetch → Epic resolution → ssw-bundle checkout → Loki + SSH dump collection → persona-driven synthesis → comment posted to Jira) without depending on Jira polling, so it ships first and de-risks P2.

**Independent Test**: Operator authors a triage persona, runs a "triage this ticket" command pointing at a real SSWCI regression-failure ticket they have access to, and confirms the resulting Jira comment has all four structured sections and at least one evidence citation that matches a real log line in the run window.

**Acceptance Scenarios**:

1. **Given** the bot is running, the triage persona exists, and `JIRA_API_TOKEN` is in secrets, **When** the operator triggers a manual triage for `SSWCI-16787`, **Then** within 5 minutes a single comment appears on the ticket containing the four structured sections.
2. **Given** the same ticket has already been triaged at the current `(issue_key, comment_seq=1)` AND no force flag is passed, **When** the operator triggers another triage, **Then** the bot reports "already triaged" via audit lookup and posts nothing new to Jira.
3. **Given** the ticket title does NOT match the `regression-test . <host> . <tc>` regex (e.g. it's a non-regression bug ticket), **When** the bot processes it, **Then** the bot skips with audit status `skipped_not_regression_failure` and posts nothing.
4. **Given** the parent Epic is missing or has no branch+commit field, **When** the bot processes the ticket, **Then** the bot skips with audit status `skipped_missing_metadata`, posts nothing, and the audit row captures which metadata field was missing.
5. **Given** the operator passes the `--force` flag for an already-triaged ticket, **When** the bot triages it, **Then** a new comment is posted whose first line explicitly identifies it as "Updated triage (supersedes earlier bot comment posted at HH:MM:SS UTC)"; the prior comment remains in ticket history.

---

### User Story 2 - Auto triage when an SSWCI regression-failure ticket is assigned to me or to DevOps (Priority: P2)

When a regression-failure ticket in the SSWCI Jira project (titled `regression-test . <host> . <TC-NNNN-...>`) is **assigned to daeyeon directly OR assigned to the DevOps Team**, the bot detects the assignment via polling and automatically produces a triage comment using the same persona-driven pipeline as P1.

**Why this priority**: This is the headline value ("내가 출근해서 보면 첫 분석이 이미 달려 있다"). The assignment-triggered model is more selective than time-windowed — only tickets that actually need attention enter the queue. It depends on P1's output pipeline working, so it lands second.

**Independent Test**: A collaborator assigns an SSWCI regression-failure ticket to daeyeon (or to the DevOps team). Within 10 minutes a triage comment appears without any operator command.

**Acceptance Scenarios**:

1. **Given** the bot is running with `[triggers.jira_assigned] enabled=true`, `allowed_projects=["SSWCI"]`, and `team_name="DevOps"`, **When** an SSWCI regression-failure ticket is assigned to daeyeon for the first time, **Then** within 10 minutes a triage comment is posted on that ticket.
2. **Given** the same conditions, **When** an SSWCI regression-failure ticket has its Team field set to "DevOps", **Then** within 10 minutes a triage comment is posted (same pipeline, audit row records `assignee_path="team"`).
3. **Given** a ticket is already assigned to daeyeon at daemon boot (it was in the queue before the bot existed), **When** the first poll cycle runs, **Then** the bot seeds `jira_assigned_state` for that ticket but does NOT emit an event (no retroactive triage). Operator can force-triage via `dev fire jira-triage --issue X --force` if they want it.
4. **Given** a ticket was previously assigned to daeyeon (triaged once), then unassigned, then re-assigned to daeyeon (or to DevOps), **When** the polling trigger observes the re-entry, **Then** `assignment_gen` increments and a fresh event is emitted; a new triage comment is posted (NOT marked as supersede — this is a distinct request instance, like the gh_review_requested re-request flow).
5. **Given** the same ticket is observed by overlapping poll cycles or replay after a daemon restart at the same `(issue_key, assignment_gen)`, **When** all observations reach the dispatcher, **Then** at most one comment is posted (dedup_token guarantees uniqueness; audit row enforces single comment).
6. **Given** a ticket is in a project NOT in `allowed_projects`, **When** the bot observes it, **Then** the trigger emits no event (JQL excludes it).
7. **Given** the bot is paused, **When** tickets are assigned, **Then** state-table transitions are recorded but no Jira comments post until unpause; on unpause each queued event is processed exactly once.
8. **Given** the daemon crashes mid-triage (e.g., during ssw-bundle checkout), **When** the daemon restarts, **Then** the in-flight outbox row is recovered, the handler re-attempts (it's `idempotent=True`), and the audit-row lookup prevents a duplicate comment if a prior partial run already posted one.

---

### User Story 3 - Persona governs triage style and integrates ssw-debugger principles (Priority: P2)

The bot's triage behavior is driven by an operator-authored persona document that encodes daeyeon's NPU debugging principles (evidence-first, layer-attribution-first, no speculation without cited logs). The persona is hot-editable.

**Why this priority**: P2 because the persona is what makes this bot specifically daeyeon's triage rather than a generic LLM dump. The user explicitly framed the feature around encoding the principles already captured in the `oh-my-debugger` plugin skills.

**Independent Test**: Operator runs a manual triage on a ticket and inspects the comment. Then operator edits the persona (e.g., adds "first observation 여부를 항상 명시" rule), triggers a fresh triage on a different ticket, and verifies the new persona rule is reflected.

**Acceptance Scenarios**:

1. **Given** a triage persona exists and the bot has triaged at least one ticket with it, **When** the operator edits the persona document and saves, **Then** the next triage reflects the edited content (no daemon restart required).
2. **Given** the persona document is missing, unreadable, or shorter than `min_persona_chars` after frontmatter strip, **When** a triage event is processed, **Then** the bot does not post a comment; the request is sent to the dead-letter list with a clear "persona unavailable" reason.
3. **Given** the operator switches `[handlers.jira_triage].persona_skill` from `daeyeon-bot-jira-triage` to a different skill name and reloads config, **When** the next triage runs, **Then** the new persona's SKILL.md is used.

---

### User Story 4 - Operator pause kill-switch (Priority: P3)

The operator can pause the bot globally so no Jira comments are posted, and unpause it later without losing pending triage events.

**Why this priority**: Operational safety net rather than core value. The kill-switch already exists in the daemon (Phase 3) — this story just verifies that the new handler honors it.

**Independent Test**: With the bot running and processing new tickets, the operator issues a pause; subsequent new-ticket events arrive but no Jira comments are posted. After unpause, the queued triages complete.

**Acceptance Scenarios**:

1. **Given** the bot is running and auto-triaging, **When** the operator issues a global pause, **Then** new triage events are accepted and queued, but no Jira comments are posted until unpause.
2. **Given** the bot is paused with several queued events, **When** the operator unpauses, **Then** each queued event is processed once (no duplicates, no losses) and the first triage comment posts within 5 minutes of unpause.

---

### Edge Cases

- **Title regex miss**: Ticket title doesn't match `regression-test . <host> . <tc>` (e.g., it's a manually-filed bug, a feature request, or a typo in the title). Bot skips with `skipped_not_regression_failure`; no comment posted. Trigger could narrow this at JQL time via `issuetype`, but title-regex is the final canonical filter.
- **Parent Epic missing branch+commit**: Some Epics may pre-date the convention or have empty fields. Bot skips with `skipped_missing_metadata` and the audit row records `missing_fields=["branch"]` (or `commit`, or both). Operator can backfill the Epic and retry via `--force`.
- **ssw-bundle commit not on origin**: The Epic references a commit (or branch tip) that doesn't exist in the public ssw-bundle remote — operator's local-only branch, force-pushed and lost, etc. `infra/ssw_bundle.py` raises after `git fetch` fails to resolve; handler skips with `skipped_unresolvable_commit`.
- **Submodule init failure**: A submodule remote rejects the bot's SSH key, or the pinned submodule commit was garbage-collected. `git submodule update --init --recursive` fails; handler captures stderr and skips with `skipped_submodule_failure`. ssw-bundle super-repo files still readable, but product code wasn't reachable — the audit row records which submodule failed.
- **Test file not found in suites tree**: The TC name from the title doesn't match any `.robot` file under `test/system/suites/`. Could be a renamed test, a typo, or a TC that lives outside the standard tree. Handler proceeds with `test_code=None` and the persona is told this section was unavailable; comment still posts (Loki + SSH dump can carry the load).
- **Loki unreachable**: `loki.ssw.rbln.in` returns connection-refused or 5xx persistently. After 3 retries the handler proceeds with `loki_streams={}`; comment still posts but tagged `[loki: unavailable]` in the Evidence section. Audit row captures `loki_error`.
- **SSH host unreachable / wrong credentials**: `automation@<host>` connect fails. Handler proceeds with `ssh_artifacts={}`; comment still posts but tagged `[ssh: unavailable]`. Audit captures `ssh_error`.
- **DNS resolution fails for hostname**: `socket.gethostbyname("<host>")` raises. Handler still queries kernel/syslog Loki (which uses hostname-by-name) and SSH (which uses hostname directly), but skips fwlog/smclog (which needs IP labels). Evidence section notes the partial coverage.
- **Start/End timestamps missing or malformed**: Body doesn't contain the expected `YYYY-MM-DD HH:MM:SS.ffffff` pair. Bot widens the Loki query window to `created_at ± 30 min` as a fallback; audit row notes `time_window_fallback=true`.
- **Run-id mismatch in SSH URL**: The `<run-id>` in the SSH URL doesn't exist on the test host (cleaned up by retention, host re-imaged). SSH `ls` returns empty; handler proceeds with empty `ssh_artifacts` and tags Evidence accordingly.
- **Force re-triage**: A new comment is posted; its first line marks it as "Updated triage (supersedes earlier bot comment posted at HH:MM:SS UTC)". Prior comment remains in ticket history (Jira does allow deleting one's own comments via API, but the bot does NOT — chronological supersede is the only mode, mirroring pr_review's decision).
- **Persona missing or invalid**: Same as pr_review — DeadLetter, no generic fallback comment.
- **Ticket closed mid-triage**: Jira ticket is closed/resolved while the bot is collecting evidence. Bot posts the comment anyway (the work is done; the comment is informative on a closed ticket too).
- **Secrets in evidence**: Loki stream or dmesg dump contains content matching the redaction regex set. The handler applies the SAME stricter-than-log redaction policy as pr_review: any match in the comment body raises `PermanentError("redaction would alter posted content")` → DeadLetter. Operator inspects, addresses the root cause (e.g., a leaked token in a test log), retries.
- **Self-loop on bot's own comments**: The bot's previously-posted triage comment must never trigger a new event (the trigger filters on `created_at`, not `updated_at`, and only watches new tickets — comments don't bump ticket creation).
- **Concurrent runs of the same TC**: Two different hosts run the same TC in parallel and both fail; two tickets are filed. The bot triages both independently (distinct issue keys → distinct dedup tokens). Each comment cites its own host's logs.

## Requirements *(mandatory)*

### Functional Requirements

#### Triggers

- **FR-001**: The bot MUST allow the operator to trigger a triage of a specified Jira ticket (issue key like `SSWCI-16787` or full URL) on demand.
- **FR-002**: The bot MUST automatically trigger a triage whenever an SSWCI ticket enters the **watched set** defined as `(assignee = currentUser() OR "Team" = "<team_name>") AND project IN allowed_projects AND summary ~ "regression-test" AND status != Closed`. "Entering" means: the ticket is observed in the watched set in poll cycle N but was NOT in pending-set as of poll cycle N-1. First-time observation OR re-entry after leaving both count.
- **FR-003**: The bot MUST NOT auto-trigger triage on any other condition (ticket created but not assigned to me / DevOps, comment added, label change, priority change, status transition that doesn't cross "Closed" boundary, etc.). Manual trigger remains the override for those cases.
- **FR-004**: The bot MUST skip auto-triage events whose title does NOT match the regression-failure regex even when JQL admitted the ticket (defense-in-depth — JQL's `summary ~ "regression-test"` is a fuzzy match and may admit borderline titles). Skip is recorded as `audit.status='skipped_not_regression_failure'`; no comment posted.
- **FR-004a**: On the very first poll after the daemon's birth (state table empty, `meta.jira_assigned_state_seeded != '1'`), the bot MUST seed `jira_assigned_state` rows for every ticket currently in the watched set with `in_pending_set=1, assignment_gen=1`, but MUST NOT emit any events. After seeding, `meta.jira_assigned_state_seeded` is set to `'1'`. This prevents thundering-herd retroactive triage on day-1 deploy.

#### Metadata resolution

- **FR-005**: The bot MUST resolve the parent Epic of each triage target via Jira REST `GET /rest/api/3/issue/{key}?expand=names,renderedFields` and extract `branch` (string, e.g. `release/v3.2`) and `commit` (40-hex SHA) custom-field values. The exact custom-field IDs are discovered at boot via `getJiraIssueTypeMetaWithFields` and cached for the daemon lifetime.
- **FR-006**: The bot MUST extract Start/End timestamps from the triage target's body via regex `(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\.\d{6})` (first two matches in the "Start/End" labelled block). If either is missing or unparseable, the bot widens the Loki window to `created_at ± 30 min` and records `time_window_fallback=true` in audit.
- **FR-007**: The bot MUST extract the SSH log-dump URL from the body via regex `ssh://automation@(?P<host>[\w.-]+):(?P<path>/mnt/data/logs/regression-test/[\w/-]+)`. If the URL is missing, `ssh_artifacts={}` and the SSH section is tagged `[ssh: not advertised]` in evidence.
- **FR-008**: The bot MUST parse the triage target's title via regex `^regression-test\s*\.\s*(?P<hostname>[\w-]+)\s*\.\s*(?P<tc>TC-\d+-\S+)\s*$`. The matched `hostname` and `tc` are the canonical run-meta even when other body sources give conflicting values.

#### ssw-bundle reproduction

- **FR-009**: The bot MUST maintain a single dedicated ssw-bundle clone at `<project_root>/var/ssw-bundle/` (config knob `[handlers.jira_triage].ssw_bundle_path`). The bot MUST NOT operate on any other ssw-bundle path the operator may have. The bot MUST refuse to start the handler if the configured path resolves outside the project root unless `[handlers.jira_triage].allow_external_ssw_bundle = true` is set explicitly.
- **FR-010**: For each triage, the bot MUST `git fetch origin`, `git checkout <commit>`, and `git submodule update --init --recursive` (in that order, single lock). On failure at any step (network, commit unresolvable, submodule init), the triage is skipped with an explicit audit status (`skipped_unresolvable_commit` / `skipped_submodule_failure`).
- **FR-011**: The bot MUST locate the TC's `.robot` file by searching `test/system/suites/**/*.robot` for a `Test Case` declaration whose name matches the `<tc>` extracted from the title (case-sensitive, exact match). If not found, the bot proceeds with `test_code=None` and tags Evidence accordingly — this is NOT a skip condition.
- **FR-012**: The bot MUST NEVER `git push`, `git commit`, or otherwise mutate the remote of the ssw-bundle clone. Only read-side and checkout-side git operations are allowed.

#### Log collection

- **FR-013**: The bot MUST query Loki at `[loki].base_url` (default `http://loki.ssw.rbln.in`) for fwlog/smclog/kernel/syslog streams, scoped to the (hostname, start_ts, end_ts) tuple. fwlog/smclog queries MUST add `test_name="<tc>"` and `hostname="<ip>"` labels; kernel/syslog queries MUST add `hostname="<name>"`. Hostname↔IP translation uses `socket.gethostbyname` with in-process cache for the current triage.
- **FR-014**: The bot MUST fetch RF artifacts from the SSH log dump directory (`/mnt/data/logs/regression-test/<run-id>/<host>/<TC>/`) via `asyncssh` using shared `automation` credentials from secrets provider key `SSW_AUTOMATION_PASSWORD`. The bot MUST fetch (at minimum, if present) `output.xml`, `dmesg.log`, and `console.log`. File size is capped at `[handlers.jira_triage].ssh_max_file_bytes` (default 10 MB) per file; oversized files are skipped with a note.
- **FR-015**: The bot MUST NOT log the value of `SSW_AUTOMATION_PASSWORD` or `JIRA_API_TOKEN` anywhere. The structlog redaction processor MUST be extended with a literal pattern matching the automation password before this feature ships.

#### Triage output

- **FR-016**: Every posted Jira comment MUST include a four-section structured body: **Symptom** (one sentence), **Evidence cited** (bullet list with source + quoted line + citation), **Likely layer** (one of `Driver`, `SysFw`, `CpFw`, `SysSol`, `DevOps`, `Connectivity`, `unknown`), **Next data to collect** (bullet list). Sections MUST appear in this order. The comment body MUST be in Korean prose with English technical terms / paths / log lines preserved verbatim.
- **FR-017**: The bot MUST NOT diagnose a root cause without a cited evidence line. The persona's "evidence before conclusion" rule is enforced via the persona system prompt; structural enforcement is via Pydantic validation requiring `len(evidence) >= 1` whenever `domain != "unknown"`.
- **FR-018**: The bot MUST post the comment via Jira REST `POST /rest/api/2/issue/{key}/comment` with a **Jira wiki markup** body (plain string, not ADF). Wiki markup is the same dialect already used in `ssw-bundle/inv/test_report/jira_markup.py` and renders consistently in Jira Cloud. The bot MUST NOT use any other Jira write endpoint — no field updates, no transitions, no link creation. The bot's write-API surface is enumerated in `contracts/jira-rest-api-surface.md`; additions require a spec amendment.
- **FR-019**: When the persona has nothing actionable (clean run, infra noise only), the bot MUST still post a comment with `domain="unknown"`, `severity="unknown"`, a single Symptom sentence, an empty Evidence list, and a Next-data-to-collect bullet. The bot MUST NOT skip silently.

#### Identity & access

- **FR-020**: The bot MUST authenticate to Jira via REST API using **HTTP basic auth** with email + API token from the daemon's secrets provider chain. The two keys are `JIRA_USER` (operator's Atlassian email) and `JIRA_API_TOKEN` — names match the convention already established in `ssw-bundle/inv/test_report/jira_client.py`. Base URL `https://rbln.atlassian.net/`. The bot MUST NOT route through the Atlassian MCP server.
- **FR-021**: The bot MUST authenticate to test hosts via SSH using `automation` username and the password from `SSW_AUTOMATION_PASSWORD`. The bot MUST support `StrictHostKeyChecking=accept-new` on first contact and write the resulting `known_hosts` entry to `<state_dir>/jira_triage_known_hosts`.
- **FR-022**: The bot MUST NOT include any of the operator's secrets, credentials, persona file paths, or local configuration paths in any posted comment.

#### Reliability & operability

- **FR-023**: The bot MUST record `(issue_key, parent_epic_key, comment_seq, head_sha, run_id, status)` for every triage attempt. The bot MUST refuse to post a duplicate comment for the same `(issue_key, comment_seq)` unless the operator explicitly forces a re-triage.
- **FR-024**: A force re-triage MUST be posted as a new Jira comment (the bot does NOT use Jira's comment-delete API even though it technically permits it — chronological supersede only). The new comment's first line MUST read: `Updated triage (supersedes earlier bot comment posted at <HH:MM:SS UTC>)`. The audit row points at the new comment's identifier; prior comment ids are appended to `superseded_comment_ids`.
- **FR-025**: The polling trigger MUST maintain `jira_assigned_state(issue_key TEXT, project TEXT, in_pending_set INTEGER, assignment_gen INTEGER, last_observed_at TEXT)` (one row per issue ever observed in the watched set) and update the row + emit-event atomically per the state machine in `data-model.md` §5. The trigger MUST emit a new event whenever any of the following occur: (a) the issue enters the set for the first time (gen=1); (b) the issue leaves the set and later re-enters (gen += 1). Each distinct trigger event MUST carry a unique `assignment_gen` counter so the dedup token derived from `(issue_key, assignment_gen)` is unique per request instance, while redundant polling observations of the same `(issue_key, assignment_gen)` MUST NOT emit a new event.
- **FR-026**: `events.source_dedup_key` for an auto-trigger event MUST be `sha256("jira-assigned|{key}|{assignment_gen}")` so that `INSERT ... ON CONFLICT(source, source_dedup_key) DO NOTHING` is the only race-safe enqueue path the trigger uses to enqueue work.
- **FR-027**: The bot MUST treat each triage event as recoverable: a crash mid-handler results in the outbox row being re-claimed on next boot or escalated to dead-letter — never silently dropped.
- **FR-028**: The bot MUST tolerate Jira / Loki / SSH transient errors with retry-with-backoff; non-retryable errors send the event to dead-letter.
- **FR-029**: The bot MUST allow the operator to pause and unpause comment posting globally without losing queued triage events (the existing `PAUSE` kill-switch applies).
- **FR-030**: The operator MUST be able to inspect, for each triage event, its current state (queued / in-progress / posted / skipped / dead-letter) along with the issue key, parent Epic, head SHA, run id, and which evidence channels succeeded.
- **FR-031**: The bot MUST cap per-event wall-clock at `[handlers.jira_triage].timeout_seconds` (default 600 s = 10 min) covering all stages (Jira fetch, ssw-bundle checkout, Loki query, SSH fetch, Claude call, comment post). Exceeded → `TransientError` → `Retry` with backoff; second timeout → DeadLetter.

### Key Entities

- **Triage Event**: A unit of work asking the bot to triage one Jira ticket at one assignment instance. Attributes: requesting source (manual / auto), issue key, parent Epic key, head SHA, branch, run-id (parsed from SSH URL or empty), `assignee_path` (`"user"` or `"team"` for auto; `"manual"` for manual), `assignment_gen` (auto-trigger: monotonic integer counter per `issue_key`; manual non-force: sentinel `"manual_0"`; manual force: sentinel `"manual_<unix_ts>"`), requested-at timestamp, status, attempt count, last error.
- **Jira-Assignment Tracking State**: Per-issue state the polling trigger maintains so it can recognize re-assignments after un-assignment. Stored as one row per issue ever observed in the watched set. Attributes: `issue_key`, `project`, `in_pending_set` flag (whether the most recent poll saw this issue still matching the JQL), `assignment_gen` (monotonic; incremented every time the issue re-enters the set), `last_observed_at` timestamp. The trigger updates this table inside the same SQLite transaction that writes `events`/`outbox` rows, so observation and emission are atomic.
- **Triage Persona**: The operator-authored Claude Code skill that encodes daeyeon's NPU triage principles. Source location: `~/.claude/skills/<name>/SKILL.md` with `daeyeon-bot/.claude/skills/<name>/SKILL.md` as fallback. Attributes: skill directory name, body content (markdown after frontmatter strip), last-modified timestamp.
- **Run Snapshot**: Everything the handler builds before calling Claude. Attributes: ticket meta (key, title, reporter), run meta (hostname, host_ip, run_id, start_ts, end_ts, branch, commit), error_log (excerpt from ticket body), test_code (contents of the `.robot` file or None), product_code (dict of file_path → contents, capped), loki_streams (dict of `fwlog`/`smclog`/`kernel`/`syslog` → list of log lines), ssh_artifacts (dict of `output_xml`/`dmesg`/`console` → string).
- **Triage Result**: The output the persona produced. Attributes: `summary_md` (4-section Korean prose), `domain` (ENUM), `severity` (ENUM), `suspected_duplicates` (list of issue keys), `needs_human` (bool), `evidence` (list of `(source, quote, citation)`).
- **Pause State**: Reused from existing daemon — no new state.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For SSWCI regression-failure tickets, an operator-triggered triage (P1) results in a posted comment within 10 minutes in 95% of attempts.
- **SC-002**: When an SSWCI regression-failure ticket is assigned to daeyeon or to DevOps Team (P2), the bot posts its triage comment within 15 minutes in 95% of cases (5 min poll cadence + 10 min handler budget).
- **SC-003**: 100% of posted comments contain the four structured sections (Symptom / Evidence cited / Likely layer / Next data to collect) in that order. 0% are posted without the structure.
- **SC-004**: When the persona produces a non-`unknown` `domain`, 100% of comments include at least one evidence citation (Pydantic constraint).
- **SC-005**: After the operator edits the persona document and saves, the very next triage (without a daemon restart) reflects the edit, verified by an operator-defined regression ticket.
- **SC-006**: The bot posts at most one comment per `(issue_key, assignment_gen | comment_seq)` tuple in 100% of cases (zero duplicates from a single request instance), regardless of overlapping polls, daemon restarts, or replay. Distinct request instances on the same issue (e.g., the ticket is re-assigned after being un-assigned) DO produce a new comment; that is the intended behavior, not a duplicate.
- **SC-007**: While paused, the bot posts zero Jira comments. After unpause, every queued triage is processed (none dropped) and the first comment posts within 10 minutes of unpause.
- **SC-008**: The bot never posts content matching the operator's known-bad secret fixture set in any comment — 100% pass rate on automated redaction checks (same regex set as `pr_review`).
- **SC-009**: The bot triages 100% of SSWCI regression-failure tickets that enter the watched set (newly assigned to daeyeon or to DevOps Team) and pass the title regex (subject to the dead-letter exceptions: persona unavailable, ssw-bundle unresolvable, missing metadata) and 0% of tickets that do not match.
- **SC-010**: When auditing 20 randomly chosen real triage comments, ≥80% are judged by the operator to contain at least one piece of actionable, evidence-grounded analysis that materially shortened the human triage step (qualitative quality bar).
- **SC-011**: The bot's ssw-bundle clone at `var/ssw-bundle/` MUST NEVER push to remote or write outside `<project_root>/var/`. Verified by an integration test that snapshots filesystem state before/after a triage.
- **SC-012**: 100% of bot-posted comments are in Korean prose with English technical terms preserved. (Validated by a script that scans audit `summary_chars` and rejects comments with no Korean characters.)

## Assumptions

- **Single operator, single tenant**: The bot runs under one operator's Jira identity (daeyeon) and posts comments as that identity. There is no separate "bot account"; comments are attributed to the operator.
- **Jira auth via basic auth (email + API token)**: The operator generates a Jira API token (`https://id.atlassian.com/manage-profile/security/api-tokens`) once. The operator's Atlassian email is stored under `JIRA_USER` and the token under `JIRA_API_TOKEN` in the daemon's secrets provider chain. This matches the existing convention in `ssw-bundle/inv/test_report/jira_client.py:11-24`. Token rotation is the operator's responsibility.
- **Jira host fixed**: `https://rbln.atlassian.net`. Multi-tenant / on-prem Jira is out of scope for v1.
- **Trigger scope**: Auto-trigger fires only on **assignment events** — a ticket entering the watched set `(assignee = currentUser() OR "Team" = "DevOps") AND project IN allowed_projects AND summary ~ "regression-test" AND status != Closed`. Comment additions, label changes, priority changes, and status transitions that don't toggle the `Closed` flag are explicitly out of scope.
- **First-release `allowed_projects`**: `["SSWCI"]` only. SNF or other projects added in a follow-up after persona tuning settles.
- **First-release `team_name`**: `"DevOps"`. Other team membership (e.g., joining HW team channel) is out of scope. Setting `team_name=""` in config disables the team match — assignee-only mode.
- **Team-overlap noise tolerated**: When a ticket is assigned to the DevOps team, multiple teammates may already be looking at it. The bot will still triage. The user explicitly accepted this in clarification — "아무에게나 초벌 분석은 도움". Comments from the bot are evidence-collection oriented; redundancy with human triage is OK.
- **Persona format**: Same as pr_review — Claude Code skill at `~/.claude/skills/<name>/SKILL.md`, frontmatter parsed-but-ignored, body as system prompt. `daeyeon-bot/.claude/skills/daeyeon-bot-jira-triage/SKILL.md` ships as the bundled default.
- **ssw-bundle remote**: `git@github.com:rebellions-sw/ssw-bundle.git`. The bot uses the operator's existing SSH key for this remote (no separate deploy key in v1; revisit if multi-host deployment is needed).
- **SSH credentials are shared-lab**: `automation:automation` works on all current SSW test hosts and the operator's PC can reach them. Switching to SSH-key auth is a follow-up tracked in RUNBOOK.
- **Internal DNS resolves test hostnames**: `socket.gethostbyname("ssw-giga-02")` works without per-host config.
- **Loki is cluster-internal, unauthenticated**: `http://loki.ssw.rbln.in` is reachable from the operator's PC; no token needed.
- **oh-my-debugger plugin installed**: The operator has `oh-my-debugger` v0.11.0 or newer installed at `~/.claude/plugins/cache/oh-my-debugger/...`. PR-2's persona references the plugin's principles by name; PR-4 (separate spec extension) will enable `/oh-my-debugger:short-triage` skill-tool invocations from the bot's Claude session.
- **No allow-list per repo**: The trigger condition is already narrow (`project = SSWCI AND issuetype = ...`); a per-component or per-host filter is not needed in v1.
- **Daemon integration**: Implemented as one handler and one trigger on top of the existing outbox / dispatcher / at-least-once delivery contract. The daemon's existing recovery, dead-letter, pause, lifecycle, and redaction semantics apply (see `CLAUDE.md`, `docs/PLAN.md`, `CONTRACTS.md`).
- **Out of scope for v1**: Triaging tickets that are not regression failures; replying to comment threads / multi-turn dialogue; modifying ticket fields/labels/priority; transitioning ticket status; linking to other tickets (auto-link to suspected duplicates is captured in `evidence` only, not in Jira link metadata); team-level Jira filters; SLA dashboards.
