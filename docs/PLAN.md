# daeyeon-bot — Implementation Plan

> **Status**: v1 (수렴 후 첫 작성, 2026-05-01)
> **Owner**: daeyeon.lee@rebellions.ai
> **Scope**: 개인용 Claude Bot daemon. 4 라운드 9 시각 설계 리뷰 거쳐 v4-final로 수렴된 골격을 기반으로 단계별 구현.

---

## 0. TL;DR

**무엇을 만드나**: macOS / Linux 사내 서버에서 daemon으로 항상 떠 있으면서, 다양한 trigger (수동 / cron / webhook / file watch / Slack 등) 를 받아 Claude (Pro/Max 구독, OAuth 토큰) 를 호출해 작업을 처리하는 개인 봇.

**무엇을 안 만드나**: SaaS, 멀티유저, API key 인증, 메시지 브로커 의존, 컨테이너 기반 분산 처리.

**왜 지금 골격을 정성껏 짜나**: trigger / handler 가 주 단위로 늘어날 예정 → 잘못된 이른 결정 하나가 6개월 뒤 전면 재작성으로 이어짐. 수렴된 v4-final 구조의 *seam* 들 (outbox / manifest / lifecycle / secrets) 만 정확히 구현해두면, 이후 모든 확장은 *덧붙이기* 만으로 끝남.

---

## 1. Goals & Non-Goals

### 1.1 Goals (3–6 개월 내)

- macOS Mac에서 launchd 로 24/7 동작
- 사내 Linux 서버 (systemd) 로 그대로 이식 가능
- 첫 trigger = manual CLI, 이후 매주 1–2 개씩 trigger / handler 추가
- 모든 Claude 호출은 본인 Pro/Max 구독으로 (API key 미사용)
- Trigger 발화 → Handler 실행 → 결과 영속화 의 흐름이 *crash-safe* (재시작해도 누락 / 중복 처리 없음)
- 설계 결함이 아닌 *코드 변경* 만으로 새 trigger / handler 추가 가능

### 1.2 Non-Goals

- 멀티유저, 멀티프로세스, 분산 처리
- 99.9% SLA / 외부 사용자 대상 응답 지연 보장
- 웹 UI / REST API (CLI + 로그로 충분)
- Docker / K8s 배포 (필요해지면 그때)
- 실시간 메트릭 (Prometheus 등) — 현 단계에선 SQLite + tail 로 충분

### 1.3 Success Criteria (Phase 5 완료 시점)

- [ ] `just install-mac` 한 번으로 fresh Mac 에서 launchd daemon 등록까지
- [ ] `just doctor` 가 토큰 / 권한 / config / 디스크 / 마이그 보류 점검
- [ ] 봇이 켜진 상태에서 `daeyeon-bot dev fire manual --message "ping"` → 30초 내 echo handler 가 Claude 응답을 events 테이블에 기록
- [ ] 강제 재시작 (`kill -KILL`) 후 in-flight 이벤트가 `interrupted` 로 마킹되어 다음 부팅 시 정책에 맞게 재개 또는 DLQ
- [ ] `daeyeon-bot inspect status` 가 outbox / in-flight / quarantined / quota 스냅샷 표시
- [ ] `tests/integration/` 의 모든 contract 테스트 통과

---

## 2. Architecture (v4-final)

### 2.1 모듈 한 줄 요약

```
core/        도메인. stdlib + dataclass 만. 외부 의존성 0.
infra/       외부 통합. SDK / SQLite / Keychain / structlog. core 에만 의존.
triggers/    Trigger 구현. core.protocols.Trigger 만족.
handlers/    Handler 구현. core.protocols.Handler 만족.
app/         조립과 수명주기. container / dispatcher / supervisor / lifecycle.
cli/         Typer 진입점. 5 파일로 묶음.
```

### 2.2 데이터 흐름

```
                 ┌─ launchd / systemd
                 ↓
         entrypoint.sh (umask 0o077, 토큰 fetch)
                 ↓
         daeyeon-bot run
                 ↓
   app/lifecycle.py:boot()
        │
        ├── config.load()              # pydantic-settings (TOML + .env)
        ├── logging.init()             # structlog + redaction + trace_id
        ├── lock.acquire()             # pidfile + flock
        ├── storage.open() + migrate() # SQLite WAL + DDL apply
        ├── secrets.load()             # Keychain / 0600 file → 메모리
        ├── permissions.probe()        # state dir / db / .env 권한 확인
        ├── container.build(cfg)       # 의존성 그래프
        ├── heartbeat.start()          # 30s 파일 touch + sd_notify
        ├── dispatcher.start()         # outbox poll loop
        ├── triggers.start_all()       # 각 trigger asyncio.Task 로
        └── wait_for_signal()          # SIGTERM / SIGINT


   Trigger
        │ emit(event)                  # source_dedup_key 포함
        ↓
   infra/outbox.py                     # INSERT INTO outbox (status='pending')
        │
        ↓
   app/dispatcher.py
        │ claim row                    # UPDATE … WHERE claimed_by IS NULL
        │ status='running'
        ↓
   TaskGroup [per-handler Semaphore]
        │
        ↓
   handlers/X.handle(event, ctx)
        │
        ├── Claude 호출 (rate-limit 통과)
        │
        └── return HandlerResult.{ACK | RETRY | DEAD_LETTER}
                │
                ↓
   infra/outbox.py:settle()
        │ status='acked' / 'retry' / 'dead_letter'
        │ runs 테이블에 결과 기록
        ↓
   다음 poll cycle
```

### 2.3 Boot 순서 (변경 금지 — `app/lifecycle.py` docstring 으로 박음)

```
1. config 로드 (pydantic-settings)
2. logging 초기화 (가능한 가장 이른 시점, 부팅 에러도 잡으려고)
3. pidfile + flock (단일 인스턴스 보장)
4. SQLite open + DDL migrations apply
5. secrets 로드 (Keychain / file → 메모리)
6. 권한 점검 (umask, state dir / db / .env chmod)
7. container build (DI 그래프)
8. heartbeat task 시작
9. dispatcher 시작 (poll loop)
10. triggers 시작 (각 task)
11. signal 대기 (SIGTERM / SIGINT)
```

### 2.4 Shutdown 순서 (2-phase, 180s budget)

```
1. signal 수신 → shutdown event set
2. Phase A: 새 이벤트 수락 중단
   - triggers.stop_all() (emit 중단)
   - dispatcher 가 더 이상 새 row 를 claim 하지 않음
3. Phase B: in-flight 드레인 (max 120s)
   - 현재 실행 중인 handler 들이 끝나거나 timeout
   - 끝난 것: 정상 status 로 settle
   - timeout 된 것: status='interrupted' 로 마킹
4. Phase C: 마무리 (max 30s)
   - heartbeat / dispatcher / outbox 닫기
   - SQLite WAL checkpoint
   - lock 해제
5. exit 0
6. 30s 추가 시간 후에도 안 끝나면 systemd / launchd 가 SIGKILL
```

---

## 3. Core Contracts

### 3.1 Delivery Semantics

- **At-least-once**. 핸들러는 동일 이벤트로 1번 이상 호출될 수 있음을 가정해야 함.
- 이를 가능하게 하려면: `HandlerManifest.idempotent = True` 선언 + `dedup_ttl` + 필요 시 `side_effect_key`.
- `idempotent = False` 인 핸들러가 `interrupted` 상태로 종료된 경우, 자동 재시도 안 함 → DLQ 로 이동, 운영자가 `replay --confirm` 결정.

### 3.2 HandlerResult (sum type)

```python
HandlerResult = ACK | RETRY(after_s: float) | DEAD_LETTER(reason: str)
```

- **ACK**: 성공. outbox status='acked'. dedup_keys 에 (event_id, handler) + TTL 등록.
- **RETRY(after_s)**: 일시적 실패. status='retry', `next_attempt_at = now + after_s`. dispatcher 가 시간 도달 후 재시도.
- **DEAD_LETTER(reason)**: 영구적 실패. status='dead_letter'. 자동 재시도 없음. `cli ops replay <id>` 로만 부활.

핸들러가 예외를 던지면:
- `TransientError` → `RETRY(default_backoff)` 와 동일 처리
- `PermanentError` 또는 분류 안 된 예외 → `DEAD_LETTER(repr(exc))` + 스택트레이스 redact 후 logs

### 3.3 Manifest 계약

```python
@dataclass(frozen=True, slots=True)
class HandlerManifest:
    name: str                       # unique, kebab-case
    idempotent: bool                # False → interrupted 시 DLQ 직행
    dedup_ttl: timedelta            # (event_id, handler) 중복 처리 방지 윈도우
    side_effect_key: str | None     # 외부 시스템 dedup 용 (예: Slack client_msg_id 필드명)
    concurrency: int                # 이 핸들러 동시 실행 상한 (asyncio.Semaphore)
    accepts: list[str]              # 처리할 event.type 목록 (라우팅 키)

@dataclass(frozen=True, slots=True)
class TriggerManifest:
    name: str
    source: str                     # outbox.source 컬럼에 들어감
    retryable_at_source: bool       # webhook 처럼 외부가 재시도 해주는지
```

### 3.4 Error 분류

```
core.errors.BotError
    ├── TransientError          # 재시도 가능 (네트워크, 타임아웃, 일시적 5xx)
    │       └── RateLimitError
    ├── PermanentError          # 재시도 무의미 (검증 실패, 4xx, 코드 버그)
    │       ├── ValidationError
    │       └── ConfigError
    ├── AuthError               # 토큰 만료/취소. 핸들러가 아니라 daemon 전체 영향.
    └── QuotaError              # 자체 rate limiter 차단
```

- `AuthError` 발생 시: dispatcher 즉시 정지, structlog ERROR, exit code 78 (`EX_CONFIG`). launchd / systemd 가 노출.
- 분류 안 된 예외 = `PermanentError` 로 간주.

### 3.5 Quota / Rate Limit

- 전역 token bucket: 기본 30 calls/hour, 200 calls/day. config 으로 오버라이드.
- 핸들러별 token bucket: manifest 또는 config 으로 지정.
- 모든 토큰 차감은 *원자적 SQL UPDATE*: `UPDATE buckets SET tokens = tokens - 1 WHERE name=? AND tokens >= 1`.
- Refill: 같은 UPDATE 안에서 `MAX(capacity, tokens + (now - last_refill) * rate)` 계산.
- Kill-switch: `~/.daeyeon-bot/PAUSE` 파일이 존재하면 Claude 호출 전에 차단.
- `cli lifecycle pause` / `resume` 이 PAUSE 파일을 만들고 지움.

### 3.6 Replay

- `cli ops replay <event_id> [--handler X] [--confirm]`
- `--confirm` 없으면 dry-run 으로 어떤 핸들러가 다시 돌지만 출력.
- 발동 시: events 의 해당 row 의 `attempt_epoch += 1`. dedup_keys 에 `(event_id, handler, attempt_epoch)` 키로 등록되므로 이전 ACK 와 충돌하지 않음.
- side_effect_key 가 있는 핸들러는 외부 시스템 dedup (예: Slack `client_msg_id`) 으로 중복 사이드이펙트 방지.

---

## 4. Data Schemas

### 4.1 SQLite DDL (`infra/db/migrations/001_init.sql`)

```sql
-- meta: 자체 버저닝 마이그레이션 추적
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO meta (key, value) VALUES ('schema_version', '1');

-- events: 모든 이벤트 (영속, 90 일 보존)
CREATE TABLE events (
    id                TEXT PRIMARY KEY,           -- UUIDv7
    type              TEXT NOT NULL,              -- 라우팅 키 (e.g., "manual.message")
    schema_version    INTEGER NOT NULL,
    source            TEXT NOT NULL,              -- trigger 이름
    source_dedup_key  TEXT NOT NULL,              -- 외부 dedup (webhook delivery id 등)
    payload_json      TEXT NOT NULL,              -- core.Event 직렬화
    trace_id          TEXT NOT NULL,
    created_at        TEXT NOT NULL,              -- ISO8601 UTC
    UNIQUE(source, source_dedup_key)
);
CREATE INDEX idx_events_created_at ON events(created_at);

-- outbox: 처리 대기/진행 상태 (events 와 1:N — 한 이벤트가 여러 핸들러로)
CREATE TABLE outbox (
    id                INTEGER PRIMARY KEY,
    event_id          TEXT NOT NULL REFERENCES events(id),
    handler           TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
                          ('pending','running','acked','retry','dead_letter','interrupted')),
    attempt           INTEGER NOT NULL DEFAULT 0,
    attempt_epoch     INTEGER NOT NULL DEFAULT 0, -- replay 시 증가
    next_attempt_at   TEXT,                       -- RETRY 시점
    claimed_by        TEXT,                       -- pid:host (claim-row)
    claimed_at        TEXT,
    last_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(event_id, handler, attempt_epoch)
);
CREATE INDEX idx_outbox_status_next ON outbox(status, next_attempt_at);
CREATE INDEX idx_outbox_claimed ON outbox(claimed_by);

-- runs: 핸들러 실행 이력 (감사용, 30 일 + 핸들러당 최근 10개 보존)
CREATE TABLE runs (
    id                INTEGER PRIMARY KEY,
    outbox_id         INTEGER NOT NULL REFERENCES outbox(id),
    event_id          TEXT NOT NULL,
    handler           TEXT NOT NULL,
    attempt_epoch     INTEGER NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,              -- acked/retry/dead_letter/interrupted
    duration_ms       INTEGER,
    triggered_by      TEXT NOT NULL DEFAULT 'dispatcher', -- 'dispatcher' | 'manual_replay'
    error             TEXT
);
CREATE INDEX idx_runs_handler_finished ON runs(handler, finished_at);

-- dedup_keys: idempotency
CREATE TABLE dedup_keys (
    key         TEXT PRIMARY KEY,                 -- f"{event_id}:{handler}:{attempt_epoch}"
    expires_at  TEXT NOT NULL
);
CREATE INDEX idx_dedup_expires ON dedup_keys(expires_at);

-- ratelimit_buckets: token bucket 영속화
CREATE TABLE ratelimit_buckets (
    name           TEXT PRIMARY KEY,              -- 'global' | f'handler:{name}'
    tokens         REAL NOT NULL,
    capacity       REAL NOT NULL,
    refill_per_sec REAL NOT NULL,
    last_refill    TEXT NOT NULL
);

-- quarantine: 5 fails / 10 min 시 trigger 격리
CREATE TABLE quarantine (
    trigger_name  TEXT PRIMARY KEY,
    quarantined_at TEXT NOT NULL,
    reason        TEXT NOT NULL
);
```

추가 마이그레이션 — `infra/db/migrations/002_gh_review_requested_state.sql`
(feature 001, PR-review bot). `events` / `outbox` 위에 얹는 두 테이블:

```sql
-- gh_review_requested_state: 폴링 트리거의 (repo, pr_number) 상태
-- 'review-requested:@me' 결과를 매 5 분마다 비교해서 "재요청"을 인식.
CREATE TABLE gh_review_requested_state (
    repo            TEXT NOT NULL,           -- "owner/repo"
    pr_number       INTEGER NOT NULL,
    head_sha        TEXT NOT NULL,           -- 최근 관측된 head commit SHA
    request_gen     INTEGER NOT NULL,        -- 단조 증가; 재요청·SHA 변경 시 +1
    in_pending_set  INTEGER NOT NULL,        -- 0/1; 마지막 폴에 PR 이 들어 있었나
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (repo, pr_number)
);
CREATE INDEX idx_grrs_pending ON gh_review_requested_state(in_pending_set);

-- pr_review_audit: 핸들러가 한 번 동작할 때마다 한 행. 강제 supersede
-- 시 row 를 갱신하고 이전 review_id 를 superseded_review_ids JSON 에 push.
CREATE TABLE pr_review_audit (
    id                       INTEGER PRIMARY KEY,
    event_id                 TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    repo                     TEXT NOT NULL,
    pr_number                INTEGER NOT NULL,
    head_sha                 TEXT NOT NULL,
    request_gen              TEXT NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN
                                 ('posted',
                                  'skipped_self_authored',
                                  'skipped_not_authored',    -- 012
                                  'skipped_withdrawn',
                                  'skipped_too_large',
                                  'skipped_already_reviewed',
                                  'skipped_disallowed_repo', -- 004
                                  'failed')),
    review_id                INTEGER,
    submitted_at             TEXT,
    summary_chars            INTEGER,
    inline_comment_count     INTEGER,
    superseded_review_ids    TEXT NOT NULL DEFAULT '[]',
    persona_skill            TEXT,
    persona_mtime_ns         INTEGER,
    error                    TEXT,
    created_at               TEXT NOT NULL
);
CREATE INDEX idx_pra_repo_pr_sha ON pr_review_audit(repo, pr_number, head_sha);
CREATE INDEX idx_pra_event ON pr_review_audit(event_id);
```

`gh_review_requested_state` 의 dormant (`in_pending_set = 0`) 행은
`retention.gh_state_dormant_days` (기본 90) 가 지나면 prune 으로 삭제.
pending 상태 행은 절대 prune 되지 않는다 (살아있는 리뷰 요청).

모든 SQLite 연결은 다음 PRAGMA 적용 (connection factory 헬퍼):
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

### 4.2 `config.example.toml` 구조

```toml
[runtime]
state_dir = "~/.daeyeon-bot"        # state.db, PAUSE, heartbeat 위치
shutdown_budget_seconds = 180

[logging]
level = "INFO"
format = "json"                      # "json" | "console"

[retention]
events_days = 90
runs_days = 30
runs_keep_per_handler = 10
dedup_default_ttl_days = 7
backup_keep = 5                      # *.bak 파일 보존 개수

[ratelimit.defaults]
global_per_hour = 30
global_per_day  = 200
handler_per_hour = 10                # 미지정 핸들러 기본값

[secrets]
provider = "keychain"                # "keychain" | "file" | "env" (dev only)
keychain_service = "daeyeon-bot"
keychain_account = "oauth_token"
file_path = "/etc/daeyeon-bot/oauth_token"

[claude]
model = "claude-opus-4-7"            # 핸들러별 오버라이드 가능
default_system_prompt = "You are daeyeon's helpful assistant."

# Trigger 선언 (이름이 곧 키)
[triggers.manual]
enabled = true

# Handler 선언 + manifest 오버라이드
[handlers.echo]
enabled = true
idempotent = true
dedup_ttl_seconds = 86400
concurrency = 1
accepts = ["manual.message"]

# 라우팅: type → [handler...]
[routing]
"manual.message" = ["echo"]
```

### 4.3 핵심 Python 타입 (요약)

```python
# core/events.py
@dataclass(frozen=True, slots=True)
class Event:
    id: str                          # UUIDv7
    type: str                        # 라우팅 키
    schema_version: int
    payload: Mapping[str, Any]       # 타입별 페이로드
    trace_id: str
    created_at: datetime             # tz-aware UTC

# core/results.py
@dataclass(frozen=True, slots=True)
class Ack: ...
@dataclass(frozen=True, slots=True)
class Retry: after_s: float
@dataclass(frozen=True, slots=True)
class DeadLetter: reason: str
HandlerResult = Ack | Retry | DeadLetter

# core/protocols.py
class Trigger(Protocol):
    manifest: TriggerManifest
    async def run(self, emit: Callable[[Event], Awaitable[None]],
                  ctx: TriggerContext) -> None: ...

E_contra = TypeVar("E_contra", bound=Event, contravariant=True)
class Handler(Protocol[E_contra]):
    manifest: HandlerManifest
    async def handle(self, event: E_contra, ctx: HandlerContext) -> HandlerResult: ...
```

---

## 5. Phased Implementation

### Phase 0 — Scaffolding (반나절)

**목적**: 빈 골격 + `just doctor` 가 "config 없음" 이라고 정확히 외침.

**산출물**:
- `pyproject.toml` (uv, ruff, pyright, pytest, deps)
- `uv.lock` 커밋
- `.python-version` (3.12)
- `justfile` (run / doctor / test / lint / format)
- `.gitignore` / `.env.example` / `config.example.toml` (템플릿만)
- `README.md` 골격
- `CONTRACTS.md` (이 PLAN 의 §3 추출)
- `src/daeyeon_bot/` 안의 빈 디렉토리/`__init__.py`
- `src/daeyeon_bot/cli/main.py` 에 `daeyeon-bot run/doctor` 가 NotImplementedError 던지는 stub
- `tests/` 디렉토리 구조 + 첫 테스트 (import smoke)

**Acceptance**:
- [ ] `uv sync` 성공
- [ ] `just lint` 통과 (빈 파일 기준)
- [ ] `just test` 통과 (1 개 smoke 테스트)
- [ ] `daeyeon-bot --help` 출력
- [ ] `daeyeon-bot doctor` 가 NotImplementedError 로 fail

---

### Phase 1 — Vertical Slice: manual → outbox → echo (1–2 일)

**목적**: 가장 얇은 end-to-end 흐름 1 가닥. **이게 돌면 이 봇은 살아 있는 거다**.

**구현**:
1. `core/events.py`, `core/results.py`, `core/manifest.py`, `core/errors.py`, `core/time.py`
2. `core/protocols.py` (Trigger / Handler / Storage / Clock / Outbox)
3. `infra/storage.py` (aiosqlite + WAL pragma + connection factory)
4. `infra/db/migrations/001_init.sql` 적용
5. `infra/outbox.py` (insert / claim-row / settle)
6. `infra/claude.py` (ClaudeSession AsyncContextManager — *fake 부터*, 실제 SDK 연결은 Phase 4)
7. `infra/logging.py` (structlog + trace_id contextvar — redaction 은 Phase 4 에서 강화)
8. `triggers/manual.py` (CLI 에서 발화)
9. `handlers/echo.py` (event 받아 ClaudeSession 으로 1번 호출, ACK)
10. `app/config.py` (pydantic-settings, TOML 파싱)
11. `app/container.py` (단일 wiring)
12. `app/dispatcher.py` (poll → claim → TaskGroup → settle)
13. `app/lifecycle.py` (boot 순서; shutdown 은 stub)
14. `cli/main.py` + `cli/lifecycle.py:run` + `cli/dev.py:fire`

**Acceptance**:
- [ ] `daeyeon-bot run` 으로 daemon 떠 있음
- [ ] 다른 터미널에서 `daeyeon-bot dev fire manual --message "hello"` 발화
- [ ] 5초 내 echo handler 가 fake Claude 응답을 events / runs 테이블에 기록
- [ ] events / outbox / runs 테이블 상태 정상 (`sqlite3 ~/.daeyeon-bot/state.db ".dump"`)
- [ ] Ctrl-C 로 깔끔히 종료 (모든 task cancel)
- [ ] integration test: fake Claude + manual trigger + echo handler end-to-end 1 회 통과

---

### Phase 2 — Reliability (1–2 일)

**목적**: crash / shutdown / 동시 실행에 안전.

**구현**:
1. `app/lock.py` (pidfile + flock)
2. `app/lifecycle.py` 의 2-phase shutdown 구현 (180s budget)
3. `app/supervisor.py` (transient backoff, permanent quarantine, 5 fails / 10 min)
4. `infra/outbox.py` 의 `interrupted` 처리: 부팅 시 status='running' 인 row 들을 `interrupted` 로 reset
5. dedup_keys 적용 (idempotent 핸들러)
6. attempt_epoch 컬럼 사용
7. `core/errors.py` 분류 정확화

**Acceptance**:
- [ ] `kill -KILL <pid>` 후 재시작 → in-flight 가 'interrupted' 로 마킹됨
- [ ] idempotent=True 핸들러는 interrupted → 자동 재시도
- [ ] idempotent=False 핸들러는 interrupted → dead_letter 로 이동
- [ ] 일부러 `TransientError` 던지는 테스트 핸들러 → exp-backoff 후 ACK
- [ ] `PermanentError` 5번 / 10 분 던지는 테스트 trigger → quarantine 마킹
- [ ] 두 번째 `daeyeon-bot run` 인스턴스는 flock 으로 즉시 실패
- [ ] shutdown 시간 budget 초과 케이스 테스트 (handler 가 200s 자는 척)

---

### Phase 3 — Operability (1 일)

**목적**: 운영자가 봇 상태를 보고 조작할 수 있음.

**구현**:
1. `cli/lifecycle.py:pause` / `resume` (PAUSE 파일 토글)
2. `cli/inspect.py:status` / `tail` / `events` (ls/get) / `triggers` (ls/unquarantine) / `handlers` (ls)
3. `cli/ops.py:doctor` / `migrate` / `replay` / `prune`
4. `cli/dev.py:call` (핸들러 단독 호출, outbox 우회) / `repl` (IPython 바인딩)
5. `app/heartbeat.py` (파일 touch + sd_notify if systemd)
6. `app/replay.py` (attempt_epoch++, audit는 runs.triggered_by 만)
7. `cli ops prune` retention 정책 적용

**Acceptance**:
- [ ] `doctor` 가 토큰 / DB / config / 마이그 보류 / 디스크 / heartbeat staleness 점검
- [ ] `inspect status` 출력 (outbox pending / in-flight / quarantined / quota)
- [ ] `pause` 후 trigger 발화해도 handler 가 안 돌고, `resume` 후 처리됨
- [ ] dead_letter 이벤트 → `replay <id> --confirm` → handler 재실행 → ACK
- [ ] `dev call echo --event-json '{"type": ...}'` 가 outbox 우회로 즉시 핸들러 호출

---

### Phase 4 — Security & Real Claude (1–2 일)

**목적**: 시크릿 격리, 실제 Claude 연결, 로그 누출 방지.

**구현**:
1. `infra/secrets.py` (`SecretsProvider` + `KeychainSecrets` + `FileSecrets`; `EnvSecrets` 는 `--insecure-env` 플래그로만)
2. `infra/claude.py` 가 secrets.provider 로 토큰 받아 SDK 호출 시 명시적 env 화이트리스트로 subprocess
3. `infra/logging.py` redaction 강화: Slack `xoxb-`, AWS `AKIA`, JWT, Anthropic OAuth `sk-ant-oat`, GitHub `ghp_`/`github_pat_` 패턴 + 엔트로피 fallback (>4.5 bits/char on 20+ chars)
4. structlog `format_exc_info` 에서 locals 캡처 끔; `sys.tracebacklimit` 설정
5. `app/permissions.py` 점검 → doctor 항목으로
6. `entrypoint.sh` 에서 `umask 0o077` + Keychain 토큰 fetch
7. SecretStr 기반 config 필드 (검증 에러 시 값 마스킹)

**Acceptance**:
- [ ] env 에 `CLAUDE_CODE_OAUTH_TOKEN` 없이도 Keychain 모드로 동작
- [ ] daemon 시작 후 `ps eww <pid>` 에 토큰 미노출 (메모리 안에만)
- [ ] 일부러 Slack 토큰 형태 문자열 로그에 흘리는 테스트 → 결과 로그에 `***REDACTED***` 만 남음
- [ ] 핸들러에서 일부러 raise → traceback 에 `token=...` 같은 local 변수 미노출
- [ ] real Claude 호출 1회 통과 (`dev call echo` 로 실제 응답 받기)

---

### Phase 5 — Deployment (1 일)

**목적**: Mac 과 Linux 양쪽에서 daemon 으로 등록.

**구현**:
1. `deploy/launchd/ai.rebellions.daeyeon-bot.plist` (KeepAlive, ThrottleInterval, StandardErrorPath)
2. `deploy/launchd/entrypoint.sh` (umask, Keychain fetch, exec)
3. `deploy/systemd/daeyeon-bot.service` (Type=notify, WatchdogSec=60, TimeoutStopSec=180, Protect*, NoNewPrivileges, LoadCredential)
4. `deploy/systemd/journald.conf` / `tmpfiles.conf`
5. `scripts/install-mac.sh` / `scripts/install-linux.sh`
6. `scripts/setup-token.sh` (claude setup-token → Keychain 저장 안내)
7. README operations 섹션 + Mac/Linux 파리티 표 + 사고 런북 2개 (corrupt SQLite / 토큰 폐기)

**Acceptance**:
- [ ] `just install-mac` 한 번 실행 → 봇이 launchd agent 로 등록 + 즉시 동작
- [ ] 재부팅 후 자동 시작 확인
- [ ] `launchctl kickstart -k` 으로 강제 재시작 후 `inspect status` 정상
- [ ] (사내 서버에 SSH 가능 시) `scripts/install-linux.sh` 로 systemd unit 등록 + `systemctl status` 정상
- [ ] `systemd-analyze security daeyeon-bot` 점수 5점 이하 (낮을수록 안전)

---

### Phase 6 — Hardening (선택, 1 일)

**목적**: 운영 잡일 자동화. Phase 5 까지 됐으면 충분히 살아남는다 — 이 단계는 *부패 방지*.

**구현**:
1. `cli ops prune` 에 retention 정책 자동 적용 (events 90d, runs 30d, dedup expired, backup last 5)
2. SQLite 자동 백업 cron (just recipe)
3. heartbeat staleness 자체 alert (파일 mtime > 90s → log ERROR)
4. 사고 런북 보강

**Acceptance**:
- [ ] `just prune` 한 번에 모든 보존 정책 적용
- [ ] `just backup` 으로 `state.db` snapshot + 오래된 백업 정리

---

## 6. Test Strategy

### 6.1 계층

```
tests/
├── conftest.py           # 픽스처 (FakeClock, FakeClaudeSession, in-memory SQLite, ...)
├── fakes/                # 정식 패키지
│   ├── clock.py          # FakeClock (시간 점프 가능)
│   ├── claude.py         # FakeClaudeSession (응답 스크립트)
│   ├── storage.py        # in-memory SQLite (실제 DDL 적용)
│   └── secrets.py        # InMemorySecrets
├── unit/                 # 단일 모듈 (core, app 의 순수 함수)
└── integration/          # 컨테이너 build → 시나리오 실행
    ├── test_at_least_once.py        # CONTRACTS §3.1
    ├── test_two_phase_shutdown.py   # CONTRACTS §2.4
    ├── test_quarantine_policy.py    # CONTRACTS §3.4
    ├── test_quota_killswitch.py     # CONTRACTS §3.5
    ├── test_replay_attempt_epoch.py # CONTRACTS §3.6
    └── test_secrets_isolation.py    # Phase 4
```

### 6.2 새 핸들러 추가 시 테스트 레시피 (`tests/README.md` 에도 같은 내용)

1. `handlers/<name>.py` 작성 + manifest
2. `app/container.py` 에 등록 라인 추가
3. `tests/unit/test_<name>.py` 에서 `FakeClaudeSession` + `FakeClock` 으로 핸들러 호출, 결과 검증
4. (필요 시) `tests/integration/test_<name>_e2e.py` 에서 실제 컨테이너 + in-memory SQLite + manual trigger 로 end-to-end 1 시나리오

### 6.3 커버리지 목표

- core / app: 90% 이상
- infra: 80% 이상 (외부 시스템 mock)
- cli: 60% 이상 (Typer command 로직)
- triggers / handlers: 케이스별 (idempotency, error path 필수)

---

## 7. Risks & Mitigations

| 리스크 | 영향 | 완화 |
|---|---|---|
| OAuth 토큰 만료 / 폐기 | 모든 핸들러 401 → quarantine 폭주 | `AuthError` = 즉시 dispatcher 정지 + exit 78. doctor 가 부팅 전 cheap call 로 검증 |
| Pro/Max quota 소진 | 본인 Claude Code 도 동시 마비 | 자체 token bucket 30/h + PAUSE kill-switch + replay 경고 |
| SQLite 파일 손상 | 모든 영속 상태 유실 | WAL + 부팅 전 `PRAGMA integrity_check` + 자동 백업 5 개 (`*.bak`) |
| Claude SDK subprocess 가 사용자 셸 env 상속 (AWS / GH 토큰 등) | 토큰 누출 | SDK 호출 시 명시적 env 화이트리스트 |
| 핸들러 폭주로 outbox 무한 성장 | SQLite 쿼리 plan 저하 | `cli ops prune` retention 30/90일, idx_outbox_status_next 인덱스 |
| schema 변경 시 기존 events replay 불가 | DLQ 무한 적재 | `event_schema_migrators` lazy 적용, replay 전 검증 |
| 같은 trigger 두 번 실행 (CLI + daemon) | 중복 사이드이펙트 | `source_dedup_key UNIQUE` + dedup_keys + `side_effect_key` 외부 dedup |
| systemd `Type=notify` watchdog timeout 잘못 설정 | 정상인데 자꾸 재시작 | WatchdogSec=60 + 30s 주기 sd_notify, 충분한 마진 |

---

## 8. Open Questions / 결정 필요

### 8.1 결정됨 (2026-05-03)

| # | 항목 | 결정 |
|---|---|---|
| Q0 | 프로젝트 이름 | `daeyeon-bot` |
| Q1 | Git 관리 | 로컬 only (이번 iteration 에선 GitHub 원격 없음). 다음 iteration 에서 원격 푸시 검토. |
| Q2 | state 디렉토리 | `~/.daeyeon-bot` |
| Q3 | Python 버전 | 3.12 (`.python-version` 고정) |
| Q4 | 첫 실제 trigger / handler | **GitHub PR self-review (S3)** — 본인이 PR 을 열면 봇이 자동으로 self-review 코멘트. Phase 4 (실제 Claude 연결 후) 에서 trigger=GitHub webhook, handler=`pr-self-review`. Phase 1–3 까지는 `manual` + `echo` 만으로 골격 검증. |

### 8.2 미결 — 추후 결정

1. **사내 서버 배포 시점**: Phase 5 와 동시 vs. Phase 6 이후. → 첫 시나리오가 GitHub webhook 인데, Mac 의 launchd 봇은 외부에서 webhook 수신 어려움 (NAT). 따라서 사내 서버에 systemd 로 배포하는 게 사실상 Phase 4–5 의 전제 조건이 됨. Phase 5 에서 Mac/Linux 동시 진행 권장.
2. **`config.toml` git 관리 방식**: 1 개 파일 commit + .env override vs. `config/local.toml` overlay. 첫 시나리오가 GitHub repo 패스/팀 정보를 담아야 해서, secret 이 아닌 환경 의존 값이 늘 것 → 2026-05-03 잠정 결정: **`config.toml` (commit 안 됨, `.gitignore`) + `config.example.toml` (commit 됨) 패턴**. overlay 는 도입하지 않음.
3. **백업 저장소**: 로컬 only vs. 원격 sync. → Phase 6 까지 미룸. 1 인 사용 / 사내 서버 RAID 가정 시 로컬 5 개 backup 으로 충분.
4. **GitHub PR review 핸들러 세부**: 어떤 repo / org 만 처리할지, 어떤 라벨일 때만 발동할지, public 코멘트 vs. summary draft 만. → S3 시나리오 본격 착수 전 (Phase 4 진입 직전) 별도 spec 문서로 결정.

---

## 9. References

- [`v4-final 골격 합의`](#) (이 문서 §2 — 4 라운드 9 시각 리뷰 결과)
- [`CONTRACTS.md`](./CONTRACTS.md) (이 문서 §3 추출본)
- Anthropic Claude Agent SDK docs
- Claude Code `setup-token` 문서
- launchd `KeepAlive` / systemd `Type=notify` 가이드

---

## Changelog

- **2026-05-01 v1**: 첫 작성. 4 라운드 리뷰 거쳐 v4-final 구조 확정 후.
- **2026-05-03 v1.1**: 결정 5건 반영 — 프로젝트명 `daeyeon-bot`, Git 로컬 only, state `~/.daeyeon-bot`, Python 3.12, 첫 실제 시나리오 = GitHub PR self-review (S3, Phase 4 착수). §8 Open Questions 분리 (8.1 결정됨 / 8.2 미결).
