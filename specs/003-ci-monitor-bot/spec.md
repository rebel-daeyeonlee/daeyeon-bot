# Feature Specification: CI Monitor / OnCall Triage Bot

**Feature Branch**: `003-ci-monitor-bot`
**Created**: 2026-06-19
**Status**: Draft
**Input**: User description: "이 bot system에 CI monitoring bot 기능을 추가한다. GitHub Actions workflow는 전혀 수정하지 않고, 외부에서 도는 봇이 CI 실패를 감지해 1차 원인 분석까지 해준다. 입력은 SSW DevOps on-call이 실제로 사는 2개 Slack 채널(`#ssw-devops-alerts`, `#ssw-devops-help`)의 CI-실패 alert. 실패 run의 로그를 `gh`로 가져오고, git으로 관리되는 OnCall LLM Wiki(`ssw-devops-oncall`)의 기존 runbook을 함께 Claude에 넣어 evidence-grounded triage를 만든 뒤, 그 alert thread에 1차 분석을 답글로 단다. 읽고-분석하고-알려주는 범위까지(read-only)."

## 위치 결정 (Build location)

이 기능은 **daeyeon-bot repo 안에 새 trigger + handler로** 구축한다 (`pr_review`·`jira_triage`와 동일 패턴). 기존 인프라를 최대한 재사용한다:

| 재사용 자산 | 출처 |
|---|---|
| outbox / dispatcher / at-least-once / dedup_keys / recovery | `infra/outbox.py`, `app/dispatcher.py` |
| SQLite WAL + 선형 마이그레이션 | `infra/storage.py`, `infra/db/migrations/` |
| secret redaction processor (slack/aws/jwt/anthropic/gh PAT + entropy) | `infra/logging.py` |
| Claude **SDK** 세션 (NOT `claude -p`) | `infra/claude.py` (`HandlerContext.claude_session_factory`) |
| persona SKILL.md mtime hot-reload | `pr_review`/`jira_triage`의 `persona_loader` |
| typed config 모델 + env override | `app/config.py` |
| supervisor quarantine / pause-guard | `app/supervisor.py`, PAUSE 킬스위치 |
| `gh` CLI subprocess 어댑터 | `infra/gh_cli.py` |
| audit 테이블 패턴 | `pr_review_audit`, `jira_triage_audit` |

새 컴포넌트: trigger `slack_ci_alert`, handler `ci_triage`, persona `daeyeon-bot-ci-triage`, 마이그레이션 `006_slack_ci_alert_state.sql` (`slack_ci_alert_state` + `ci_triage_audit`), Slack 어댑터 `infra/slack.py`, OnCall wiki 어댑터 `infra/oncall_wiki.py`.

## Clarifications

### Session 2026-06-19

- **Q: trigger 소스 → A:** GitHub PR required-check polling을 **하지 않는다.** 입력은 SSW DevOps on-call이 이미 모니터링하는 **2개 Slack 채널의 CI-실패 alert**다. `#ssw-devops-alerts`(C09SEN8MH5M, 자동 alert) + `#ssw-devops-help`(C0A406KREHF, 사람 요청 + `sukju-bot` CI 실패 알림). 봇은 그 alert를 *재발견*하지 않고 **이미 떨어진 alert를 입력**으로 받는다. 근거: oncall의 실제 워크플로가 이 두 채널에서 굴러가고, alert thread에 사람이 "원인/조치" 답글을 다는 패턴(그것도 이미 `@Claude`로)이 봇이 자동화할 정확한 출력이다.

- **Q: 채널별 신호 품질과 파싱 → A:** (2026-06-19 live 확인 R1 완료) 채널에 봇이 여러 개고 파싱 난이도가 갈린다.
  - **`#help`의 `sukju-bot`** = 평문 + 구조화. `PR` / `head SHA` / `실패 job` / `실패 run`(actions/runs URL) / `연속 실패 N회` / `직전 동일 증상 PR`. → **파싱 거의 공짜.** v1.
  - **`#alerts`의 `dev_syssw_test` (U069J27G2G6)** = mrkdwn 평문. `*Workflow:* <actions/runs/<id>|Run Link>` + Phase + Runner + PR# + **hostname·시간창이 박힌 Grafana/Loki 링크**까지 준다. → 평문 파싱, run URL + Loki 윈도우 확보. v1.
  - **`#alerts`의 `SSW-Alert-Bot` (U09RJGLLPLZ)** = **Grafana alerting → Slack** 연동. content가 top-level `text`도 `blocks`도 아닌 **레거시 `attachments[]`** 에 있다 (그래서 MCP·blocks 모두 빈값이었음). raw `conversations.history`로 확인됨(2026-06-19): `attachments[0].title="CIHealthcheckJobFailed - firing"`, `attachments[0].text`에 severity + alert명 + **Summary("Healthcheck job CR03-premerge-phase2 failed on dev — <actions/runs/...링크>")**. → **파싱 가능, run 링크 있음. v1.** alert-rule 기반이라 healthcheck/daily 실패(R4 일부)도 여기로 들어온다.
    - **증거 경로 차이**: 일부 alert(특히 device-level CP/SMC FW fail, 예 `0x50555746 device unreachable`)는 GitHub run 로그보다 **host + fwlog/Loki가 핵심 증거**다. run 링크 있으면 `gh --log-failed`, 없거나 부족하면 **host+시간창으로 Loki 조회**(feature 002 `jira_triage` Loki 어댑터/`reference_loki` 재사용). handler는 두 경로를 모두 가진다.
  - **파서 규칙(공통)**: Slack 메시지 텍스트는 `text` / `attachments[].{title,text,fallback}` / (향후) `blocks[].text` **세 군데에 흩어질 수 있다** → 셋을 합친 뒤 `actions/runs/<id>`·Grafana/Loki 링크를 regex로 추출. 한 곳만 보면 SSW-Alert-Bot을 놓친다.
  - **필터**: 봇은 **(a) 알려진 봇 작성자(`sukju-bot`/`dev_syssw_test`/`SSW-Alert-Bot`) 메시지 또는 (b) `github.com/.../actions/runs/<id>` 링크를 가진 메시지**만 CI-실패 후보로 본다. 그 외(사람 잡담)는 무시.

- **Q: Slack 인증/접근 → A:** Slack Web API를 `httpx`로 직접 호출(jira 어댑터와 동일 컨벤션, 신규 런타임 의존 없음). 읽기 `conversations.history`(+ `conversations.replies` for thread), 쓰기 `chat.postMessage`(thread reply). 팀의 기존 `oncall-collect-slack`은 **Slack MCP(interactive 전용, headless/cron 불가)**라 daemon에선 못 쓴다 → daemon은 **자체 토큰**을 secrets provider chain(keychain → 0600 file → env w/ `--insecure-env`)으로 주입받아 직접 호출한다. `infra/logging.py` redaction이 `xox*`를 이미 스크럽. Slack **MCP는 핫패스에 두지 않는다**(jira 결정과 동일).
  - **토큰 종류 = bot token (`xoxb-`) 확정.** user token(`xoxp-`)은 **워크스페이스 정책상 admin이 승인하지 않음**(2026-06-19 확인) → 폐기. 기존 `dev_syssw_test` bot(U069J27G2G6, B069MP0MFL5) 토큰 사용. scope 보유 확인됨: `chat:write` + `chat:write.public`(public 채널 조인 없이 게시) + `chat:write.customize`(메시지별 username/아이콘 override) + `channels:history`(읽기). 읽기는 봇이 채널 멤버여야 함(`not_in_channel`) → 두 채널에 봇 초대 완료(2026-06-19). 게시는 `chat:write.public`으로 조인 없이도 가능.
  - **attribution 결과**: triage는 **봇 이름으로** 게시된다(진짜 daeyeon 계정 attribution은 정책상 불가). `chat:write.customize`로 표시명/아이콘을 전용 페르소나(예: "CI Triage Bot")로 세팅해 "자동 1차 분석"임을 명확히 한다. 푸터에 "🤖 automated first-pass (daeyeon-bot)" + run/wiki 링크. ⇒ oncall 볼트의 attribution은 게시자 신원이 아니라 **본문에 명시된 책임/owner_area**로 보존한다.

- **Q: trigger 상태/발사 조건 → A:** `gh_review_requested`/`jira_assigned` 패턴 미러링. 채널별 **읽기 커서**(마지막 처리 message ts)를 `slack_ci_alert_state`에 둔다. 신규 메시지 중 CI-실패 후보를 emit. dedupe 토큰 = `sha256("slack-ci-alert|{channel_id}|{message_ts}")`. **Cold-start**: 첫 부팅 시 각 채널의 현재 최신 ts를 커서로 시드만 하고 과거 메시지는 retroactive triage 하지 않는다(기존 큐 소급 금지 — jira 패턴과 동일). 한 alert가 동일 run을 가리키면 `repo+run_id` 보조 dedupe로 중복 triage 방지.

- **Q: 액션 범위 → A:** **alert thread에 코멘트 1건 추가**가 유일한 쓰기. rerun 트리거·PR comment·label·issue 생성·branch protection·Slack reaction/이외 채널 전송은 v1에서 하지 않는다(pr_review가 COMMENT 외 review event를 절대 안 쓰는 정책과 동일). 멱등성은 `ci_triage_audit` 행 lookup으로 — 동일 `(channel_id, message_ts)`에 중복 POST 방지. **PoC 초기에는 실 alert 채널 오염 방지를 위해 별도 테스트 채널로 단방향 전송(`ci_triage.dry_run_channel`)으로 시작하고, 검증 후 원본 thread reply로 승격**(config 토글).

- **Q: 로그 수집 → A:** alert에서 추출한 run id로 `gh run view <run_id> --repo <repo> --log-failed`(read-only). 실측: 한 run이 **438KB / 3000+줄**, head 25줄은 `GITHUB_TOKEN Permissions` 보일러플레이트, 진짜 원인은 중간에 묻힘. 따라서 (1) **ANSI 컬러코드 strip** → (2) **redaction** → (3) **error-anchored truncation**: `##[error]` / `Process completed with exit code` / `ERROR -` / `FAIL` / "test failed" 라인 주변 N줄 윈도우만 수집(head/tail 금지). 한 run = 다중 실패 job이므로 실패 job 경로별로 윈도우를 모은다. gh 인증은 운영자 로컬 `gh` CLI(이미 인증됨).

- **Q: OnCall Wiki(지식베이스) → A:** `rebellions-sw/ssw-devops-oncall` (Obsidian 볼트). 봇은 자기 클론(`var/ssw-devops-oncall/`, gitignored)을 두고 매 이벤트 시작 시 `git pull --ff-only`로 최신화(**read-only — 수정/commit/push 절대 금지**, jira_triage의 ssw-bundle 가드 패턴 재사용). 운영자의 `~/ssw-devops-oncall`은 건드리지 않는다. 검색은 **vector DB/RAG 없이 ripgrep 키워드**로 충분.
  - **검색 스코프**: `wiki/oncall/incidents/*.md` + `wiki/notes/recovery-playbook.md`만 (shifts/people/lint-report 제외 — 노이즈).
  - **매칭 우선순위**: 로그 에러 시그니처를 incident frontmatter `signature:` 필드에 **우선 매칭**(이 볼트가 signature 재발 매칭용으로 설계됨) → 그다음 본문 → 다단어 에러 구절에 가중치(단어 하나 "qemu"는 13개 파일 매칭하는 노이즈, "VM creation failed"는 incident 1개로 정확히 좁힘). `recovery-playbook.md`(증상→조치 인덱스)는 **항상 포함**.

- **Q: Claude 입력 3종 → A:** (1) **alert metadata**(channel, repo, PR#, head SHA, 실패 job, run URL, 연속 실패 횟수, 직전 유사 PR — sukju-bot가 주는 만큼), (2) **error-anchored 실패 로그**(redact+truncate), (3) **wiki snippet**(signature 매칭 incidents + recovery-playbook). 프롬프트 규칙(필수): **로그가 1차 근거, wiki는 보조.** wiki가 검색됐다고 단정 금지 — 로그에 실제 매칭 근거가 있을 때만 wiki와 연결. 근거 부족 시 `unknown` / `confidence: low`. Claude는 SDK 세션(`ctx.claude_session_factory()`)으로 호출, 2회 시도 후 파싱 실패면 `PermanentError`→DLQ(jira_triage 패턴).

- **Q: persona → A:** pr_review/jira_triage와 동일. 번들 기본본 `daeyeon-bot/.claude/skills/daeyeon-bot-ci-triage/SKILL.md`, 사용자 홈 override, `[handlers.ci_triage].persona_skill="daeyeon-bot-ci-triage"`로 선택, 매 이벤트 mtime stat → 변경 시 재읽기. 출력 언어: **한국어 산문 + 영어 기술어/경로/로그 원문**(jira_triage·oncall 톤과 동일, 팀 내부 소통).

- **Q: Claude 출력 계약 → A:** oncall의 사고방식에 정렬한다.
  - **최상위 필드 = 귀속 판단** `attribution: infra_env | product_regression | flaky | unknown`. oncall의 incident 귀속 규칙(인프라/환경 → oncall 소유+SDOC; 제품 commit 회귀 → PR 작성자 라우팅)을 반영. "내가 복구/티켓 칠 건가, 작성자로 보낼 건가"를 한 줄로.
  - **분류** `classification: infra | environment | test_failure | device_failure | build_failure | dependency | timeout | flaky | permission | unknown`.
  - **담당 영역** `owner_area` = wiki `domain` enum과 **정렬**: `DevOps | SysFw | SysSol | Connectivity | Driver | HW | Unknown`.
  - **confidence** `low | medium | high` (wiki와 동일 어휘, 초기 진단은 `low`).
  - 그 외: 요약, 로그 근거(인용), wiki 매칭 여부(어느 incident/SDOC), 추정 원인, 기존 대응법(playbook), 권장 액션, **재시도 판단(rerun_advice)**.

- **Q: Slack 출력 형식 → A:** 사람이 바로 판단할 **요약만**: repo · PR · 실패 check/job · 실패 run 링크 · triage 요약 · attribution · classification · confidence · wiki 매칭(SDOC/incident 링크) · 권장 액션 · 재시도 판단. **상세 로그 전문/프롬프트 전문은 Slack에 넣지 않고** daemon local log(redacted)에 남긴다. 메시지 길이 초과 시 중간 truncate.

- **Q: read-only 원칙 → A:** 코드 레벨로 강제(쓰기 호출을 만들지 않음). GitHub: PR/check/run-log 조회만. OnCall Wiki: clone/`pull --ff-only`만. Slack: 지정된 코멘트 1건 전송만. 자동 rerun/티켓 생성/wiki 갱신 없음.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — 특정 실패 run을 수동 triage (Priority: P1)

운영자가 실패한 GitHub Actions run(또는 그 run을 가리키는 Slack alert message)을 봇에 직접 지정하면, 봇이 persona 기반 triage를 만들어 지정된 채널/스레드에 1건 게시한다. 게시물은 attribution / classification / confidence / 로그 근거 / wiki 매칭 / 권장 액션 / 재시도 판단을 포함한다.

**Why this priority**: 이 슬라이스 없이는 아무것도 의미 없다. 전체 파이프라인(run id → `gh --log-failed` → ANSI strip/redact/anchor-truncate → oncall wiki signature 매칭 → persona synthesis → Slack 게시)을 Slack polling 의존 없이 검증해 P2를 de-risk 한다.

**Independent Test**: 운영자가 `dev fire ci-triage --repo <r> --run <id> [--channel <test>]`로 실제 실패 run을 지정 → 결과 메시지가 4개 핵심 필드(attribution/classification/confidence/권장액션) + 로그 라인 1개 이상 인용을 포함하고, wiki 매칭이 있으면 해당 incident/SDOC 링크를 단다.

**Acceptance Scenarios**:

1. **Given** 봇이 돌고 persona가 존재하고 Slack bot token이 secrets에 있고 `gh`가 인증됨, **When** 운영자가 실제 실패 run으로 수동 triage를 트리거, **Then** 5분 내 지정 채널에 triage 1건이 게시되고 최소 1개 로그 근거 인용을 포함한다.
2. **Given** 같은 run이 이미 triage됨(`(channel_id, message_ts)` audit 존재) AND force 미지정, **When** 다시 트리거, **Then** audit lookup으로 "이미 triage됨" 보고하고 아무것도 게시하지 않는다.
3. **Given** run 로그에서 어떤 wiki signature도 매칭 안 됨, **When** 봇이 처리, **Then** `confidence: low` + wiki 매칭 "없음"으로 게시하고 로그 근거만으로 추정 원인을 단다(과단정 금지).
4. **Given** `--log-failed`가 438KB로 큼, **When** 봇이 처리, **Then** Claude 입력은 error-anchored 윈도우로 truncate되고(전문 아님) 민감 문자열은 redact되며, 전문은 daemon local log에만 남는다.
5. **Given** 운영자가 force 지정, **When** 이미 triage된 run을 다시 triage, **Then** 새 메시지 첫 줄이 "Updated triage (supersedes earlier bot comment posted at HH:MM:SS UTC)"로 명시되고 이전 메시지는 보존된다.

### User Story 2 — Slack alert가 뜨면 자동 triage (Priority: P2)

`#ssw-devops-alerts` 또는 `#ssw-devops-help`에 CI-실패 alert(`sukju-bot` 구조화 메시지 또는 `actions/runs` 링크 포함 메시지)가 올라오면, 봇이 polling으로 감지해 P1과 동일 파이프라인으로 triage를 만들어 그 alert thread에 답글을 단다.

**Why this priority**: 헤드라인 가치("alert가 뜨면 첫 분석이 이미 thread에 달려 있다"). P1 파이프라인에 의존하므로 두 번째.

**Independent Test**: 누군가 `#help`에 `sukju-bot` CI-실패 메시지를 올린다(또는 run 링크 포함 메시지). 운영자 명령 없이 10분 내 그 thread에 triage 답글이 달린다.

**Acceptance Scenarios**:

1. **Given** `[triggers.slack_ci_alert] enabled=true`, 두 채널 ID 설정됨, **When** `#help`에 `sukju-bot` CI-실패 메시지가 처음 올라옴, **Then** 10분 내 그 thread에 triage 답글이 게시된다.
2. **Given** 같은 조건, **When** `#alerts`에 `SSW-Alert-Bot` block alert가 올라옴(run 링크가 block에 있음), **Then** run 링크가 추출되면 triage가 게시되고, 추출 불가면 audit `skipped_no_run_link`로 기록하고 아무것도 게시하지 않는다.
3. **Given** `#help`에 사람 잡담 메시지(봇 작성자 아님, run 링크 없음), **When** poll, **Then** CI-실패 후보가 아니므로 emit하지 않는다.
4. **Given** 첫 부팅(cold-start), **When** 첫 poll, **Then** 각 채널 현재 최신 ts를 커서로 시드만 하고 과거 메시지를 triage하지 않는다.
5. **Given** 같은 alert가 중첩 poll/재시작 replay로 dispatcher에 여러 번 도달, **When** 처리, **Then** dedup_token + audit가 정확히 1건 게시를 보장한다.
6. **Given** 봇이 PAUSE 상태, **When** alert가 뜸, **Then** state 커서는 갱신되나 unpause까지 Claude 호출·게시는 없고, unpause 후 큐된 이벤트가 정확히 1회씩 처리된다.
7. **Given** 두 alert가 같은 `repo+run_id`를 가리킴, **When** 처리, **Then** 보조 dedupe로 run당 1회만 triage한다.

## Functional Requirements *(mandatory)*

- **FR-001**: 봇은 GitHub Actions workflow 파일을 수정하지 않는다(완전 외부 관찰자).
- **FR-002**: trigger는 `#ssw-devops-alerts`·`#ssw-devops-help`를 polling하고, **봇 작성자 메시지 또는 `actions/runs/<id>` 링크 포함 메시지**만 CI-실패 후보로 emit한다.
- **FR-003**: 채널별 읽기 커서를 `slack_ci_alert_state`에 두고, cold-start 시 과거 메시지를 소급 triage하지 않는다.
- **FR-004**: handler는 alert에서 `repo` + `run_id`(+ 가능하면 PR#/head SHA/실패 job/연속실패횟수)를 추출한다. `#help/sukju-bot`는 평문 파싱, `#alerts/SSW-Alert-Bot`는 Block Kit 파싱.
- **FR-005**: `gh run view <run_id> --repo <repo> --log-failed`로 실패 로그를 read-only 수집한다.
- **FR-006**: 로그는 (1) ANSI strip → (2) secret redaction(`infra/logging.py` 재사용) → (3) error-anchored truncation 순으로 가공해 Claude에 넣는다. 전문은 Slack에 넣지 않고 redacted local log에만 남긴다.
- **FR-007**: OnCall Wiki는 `git pull --ff-only`로만 최신화하고 절대 쓰지 않는다. 검색 스코프는 `incidents/` + `recovery-playbook.md`, `signature:` frontmatter 우선 매칭, `recovery-playbook.md` 항상 포함.
- **FR-008**: Claude 프롬프트는 "로그=1차 근거, wiki=보조, 근거 없으면 unknown/low"를 강제한다.
- **FR-009**: 출력은 `attribution`(최상위) / `classification` / `owner_area`(wiki domain enum) / `confidence`(low|medium|high) / 요약 / 로그 근거 / wiki 매칭 / 권장 액션 / rerun_advice를 포함한다.
- **FR-010**: 봇은 alert thread(또는 PoC 테스트 채널)에 코멘트 1건만 게시한다. 다른 GitHub/Slack/wiki 쓰기는 하지 않는다.
- **FR-011**: 멱등성 — 동일 `(channel_id, message_ts)`(보조: `repo+run_id`)에 대해 정확히 1회 게시한다(`ci_triage_audit` + dedup_keys).
- **FR-012**: Slack/gh/wiki 접근 실패는 typed error(`TransientError`/`PermanentError`)로 분류해 dispatcher 정책(retry/DLQ)을 따른다. AuthError는 daemon halt(exit 78).
- **FR-013**: `[handlers.ci_triage].enabled=false`로 랜딩(다른 두 핸들러처럼 opt-in). PAUSE 킬스위치를 존중한다.

### Key Entities

- **slack_ci_alert_state**: `(channel_id PK)` + `last_seen_ts` + `seeded`(cold-start 플래그) + `updated_at`. 채널별 읽기 커서.
- **ci_triage_audit**: `id` / `event_id`(FK→events) / `channel_id` / `message_ts` / `repo` / `run_id` / `pr_number` / `failed_jobs` / `status`(`posted`|`skipped_no_run_link`|`skipped_not_ci_failure`|`skipped_already_triaged`|`failed`) / `attribution` / `classification` / `owner_area` / `confidence` / `wiki_matches` / `posted_message_ts` / `summary_chars` / `persona_skill` / `persona_mtime_ns` / `gh_error` / `wiki_error` / `error` / `created_at`.

## Success Criteria *(mandatory)*

- **SC-001**: workflow 무수정으로, Slack에 뜬 CI-실패 alert를 봇이 감지한다.
- **SC-002**: 실패 run의 로그를 가져와 error-anchored로 가공한다.
- **SC-003**: OnCall Wiki에서 관련 runbook을 signature 우선으로 찾아 prompt에 부착한다.
- **SC-004**: Claude가 로그(1차)+wiki(보조) 근거로 triage를 만들고, 근거 부족 시 unknown/low로 답한다.
- **SC-005**: Slack 메시지만으로 oncall이 "infra냐 회귀냐 / 누가 소유하나 / rerun 가능한가"를 판단할 수 있다(attribution 필드).
- **SC-006**: 같은 alert/run에 중복 게시하지 않는다.
- **SC-007**: 검증 사례 — 2026-06-17 `qemu-golden-base-image-missing`(SDOC-13)류 실패에서, 봇이 `rsync ... golden-base / VM creation failed` 시그니처로 해당 incident를 매칭해 "infra_env / golden image 재빌드" 권장을 낸다.

## Out of Scope (v1)

Hermes 통합 · GitHub App/webhook 구조 · vector DB/semantic search · 자동 PR comment · 자동 rerun · 담당자 mention · 통계 대시보드 · 신규 wiki 문서 자동 생성 · GitHub PR required-check polling(이 모델에서 제거) · `#help` 사람 잡담의 일반 응답.

## Open Questions / Research

- **R1 (#alerts 파싱)**: ✅ **완전 해소(2026-06-19, raw API 확인).** 3개 소스 전부 파싱 가능 — sukju-bot/dev_syssw_test는 top-level `text`, SSW-Alert-Bot은 `attachments[].text`(Grafana alerting, run 링크 Summary에 포함). 파서는 `text`+`attachments[]`(+`blocks`) 합쳐 regex. 셋 다 v1.
- **R2 (게시 승격 시점)**: PoC 테스트 채널 단방향 → 원본 thread reply 승격을 언제/어떻게(config 토글 + 운영자 확인).
- **R3 (Slack scope/토큰)**: (2026-06-19 live 확인)
  - ✅ **두 채널 모두 public** (`is_private:false`) → 읽기 scope는 **`channels:history`** 하나면 충분 (`groups:history` 불필요).
  - ✅ **토큰 = bot token 확정.** user token은 **정책상 admin 미승인**(App approval ON, user-token app 승인 안 함) → 폐기. 기존 `dev_syssw_test` bot token 사용, 두 채널 초대 완료. scope 확인: `chat:write`+`chat:write.public`+`chat:write.customize`+`channels:history`. 게시=봇 이름(+customize 페르소나).
  - ✅ 읽기 동작 확인: 봇 초대 후 `conversations.history` 성공.
- **R4 (daily/healthcheck 확장)**: oncall 실부하의 큰 비중인 daily regression·healthcheck run은 v1 범위 밖(PR/alert 기반). 동일 메커니즘으로 v2 확장 가능 — 별도 trigger 또는 alert 소스 추가.
