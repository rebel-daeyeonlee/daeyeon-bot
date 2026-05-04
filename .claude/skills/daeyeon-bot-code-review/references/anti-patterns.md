# Anti-Patterns Catalog

리뷰에서 인용 가능한 룰 카탈로그. **여기 있는 ID/이름만** 인용한다 — 창작 금지. 적합 ID가 없으면 평문으로 룰을 서술하고, 반복 출현 시 카탈로그 추가 제안.

순서:
1. [Clean Code — Naming `[N*]`](#clean-code--naming-n)
2. [Clean Code — Functions `[F*]`](#clean-code--functions-f)
3. [Clean Code — General `[G*]`](#clean-code--general-g)
4. [Clean Code — Comments `[C*]`](#clean-code--comments-c)
5. [Pipeline Reliability `[P*]`](#pipeline-reliability-p)
6. [Test Determinism `[T*]`](#test-determinism-t)
7. [Secret & Runner Safety `[S*]`](#secret--runner-safety-s)
8. [Observability `[O*]`](#observability-o)
9. [Infrastructure-as-Code `[I*]`](#infrastructure-as-code-i)
10. [NPU Lab `[L*]`](#npu-lab-l)
11. [Spec / Process Drift `[D*]`](#spec--process-drift-d)

---

## Clean Code — Naming `[N*]`

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[N1]` | Descriptive names | MINOR (MAJOR if API) | `data`, `info`, `tmp`, `obj`, `mgr`, `helper`. 의미 없는 일반어. |
| `[N2]` | Names at appropriate abstraction level | MINOR | 저수준 모듈에 high-level 도메인 이름, 또는 그 반대. |
| `[N3]` | Standard nomenclature | MINOR | 팀 컨벤션과 다른 단어 (`fetch` vs `get`, `delete` vs `remove`). |
| `[N4]` | Unambiguous names | MAJOR | `process()` / `handle()` / `check()` — 동사가 모호. |
| `[N5]` | Long names for long scopes | MINOR | 모듈/전역 심볼이 너무 짧음 (`x`, `s`, `n`). |
| `[N6]` | Avoid encodings | MINOR | 헝가리언, `_ptr`, `i_count` 같은 타입 인코딩. |
| `[N7]` | Names describe side effects | MAJOR | `get_X()`인데 캐시 갱신/네트워크 호출. **자주 잡힌다.** |

**Pet peeve**: `get_*`이 부수효과를 가짐 → `[N7]`로 즉시 MAJOR.

---

## Clean Code — Functions `[F*]`

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[F1]` | Too many arguments | MAJOR (≥4) | 인자 4개 이상이면 dataclass / config 객체로 묶기. |
| `[F2]` | Output arguments | MAJOR | 인자를 mutate해서 반환 대신 사용. 불변성 위반. |
| `[F3]` | Flag arguments | MINOR | `do_thing(force=True)` 같은 boolean flag. 함수 분리 권장. |
| `[F4]` | Dead function | MINOR | 어디서도 호출 안 됨. 삭제. |

**Pet peeve**: output argument(`[F2]`) — 불변성 원칙(global rule) 위반이라 자동 MAJOR.

---

## Clean Code — General `[G*]`

이 표는 사용자가 실제로 인용하는 ID만 추려놓은 것. 번호가 비어 있는 자리(`[G1]`, `[G4]`, `[G6]`–`[G8]`, `[G10]`–`[G24]` …)는 *비어 있는 게 아니라 인용 대상이 아닌 것* — 임의로 채워 넣어 새 룰을 만들지 말 것 (line 3의 "창작 금지"가 그대로 적용된다). 새 ID가 필요하면 카탈로그 확장 제안.

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[G2]` | Obvious behavior unimplemented | MAJOR | 함수 이름이 약속한 동작을 구현하지 않음. |
| `[G3]` | Incorrect behavior at boundaries | CRITICAL | off-by-one, empty list, None, 빈 string 처리 누락. |
| `[G5]` | Duplication | MAJOR | 같은 로직 3번 이상 복붙. 1–2번은 OK (premature abstraction 회피). |
| `[G9]` | Dead code | MINOR | 도달 불가 코드 / 사용 안 하는 import. (Silent try/except는 `[G35]`로 분리.) |
| `[G25]` | Replace magic numbers | MINOR | `sleep(5)`, `timeout=30`, `retry=3` — 이름 없는 상수. **CRITICAL only when** the magic number is `timeout-minutes` / `retry` count / rate-limit on a regression-blocking job (회귀가 그 값에 직접 묶여 있음). |
| `[G28]` | Encapsulate conditionals | MINOR | 복잡한 boolean을 명명된 함수로. |
| `[G30]` | Functions do one thing | MAJOR | 한 함수가 fetch + parse + write. 분리. |
| `[G34]` | Descend one level of abstraction | MAJOR | 한 함수에 여러 추상화 레벨 혼재 (`http.get()` 옆에 `int(x[0]+1)`). |
| `[G35]` | Swallowed exception | CRITICAL | bare `except:` 또는 `except Exception: pass` — log/raise/typed-error 변환 없음. observability gap + silent failure. narrow exception type + structured log + propagate(or typed `Retry`/`DeadLetter`) 필수. |

**Pet peeve**:
- `try: ... except Exception: pass` → `[G35]` 즉시 CRITICAL. daily regression이 silent 실패로 며칠 가는 1순위 원인.
- magic number가 regression-blocking job의 `timeout-minutes` / `retry` / rate-limit이면 → `[G25]` CRITICAL (그 숫자가 flake source).

---

## Clean Code — Comments `[C*]`

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[C1]` | Inappropriate information | MINOR | 주석에 changelog / 작성자 / 날짜. git이 안다. |
| `[C2]` | Obsolete comment | MINOR | 코드와 일치하지 않는 주석. |
| `[C3]` | Redundant comment | MINOR | `i += 1  # increment i`. |
| `[C4]` | Poorly written comment | MINOR | 무슨 뜻인지 불분명. |
| `[C5]` | Commented-out code | MAJOR | 주석 처리된 코드. 삭제. git이 안다. |

**Pet peeve**: changelog 주석(`[C1]`) — `# 2026-05-04 daeyeon: fix flaky test`. 즉시 삭제 권장.

---

## Pipeline Reliability `[P*]`

CI/CD pipeline · GitHub Actions · build/test orchestration. **DevOps 시점에서 가장 자주 보는 영역.**

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[P1]` | Silent failure mask | CRITICAL | `continue-on-error: true`, `<code>\|\| true</code>`, `set +e` — 실패가 green으로 보임. 명확한 사유 없이 사용 시. |
| `[P2]` | Missing job timeout | MAJOR | `timeout-minutes` 누락 → runner 영구 점유 위험. |
| `[P3]` | Non-idempotent step | MAJOR | re-run 시 다른 결과 (artifact 덮어쓰기 충돌, side effect 누적). |
| `[P4]` | Hard-coded host/path | MAJOR | `/home/runner/...`, hostname literal — 자체호스트 runner 환경 의존. |
| `[P5]` | Cache key incorrect | MAJOR | lockfile 변경 추적 누락. cache poisoning 또는 stale build. |
| `[P6]` | Missing retry on transient | MINOR (MAJOR if external API) | network/registry 호출에 재시도 없음. |
| `[P7]` | Retry without idempotency | CRITICAL | `[P6]`을 fix하면서 idempotent하지 않은 step에 retry 붙임. duplicate side effect. |
| `[P8]` | Build time budget regression | MAJOR | 단일 변경으로 빌드 시간 ≥ 20% 증가 (의도 없이). |
| `[P9]` | Required check bypass | CRITICAL | branch protection 우회 / `force-merge` / required job 비활성화. |
| `[P10]` | Concurrent job race | MAJOR | 동일 자원에 대한 `concurrency:` group 누락. shared state corruption. |
| `[P11]` | Step output as state | MAJOR | step output에 sensitive 또는 multi-line state를 담아 전달. |
| `[P12]` | Missing strict shell flags | MAJOR | CI/runner script가 `set -euo pipefail` 없이 실행. 중간 실패가 silent하게 다음 step으로 흘러감. |
| `[P13]` | GHA matrix fail-fast misuse | MAJOR | `fail-fast: true` (default) on regression matrix → 한 NPU device 실패가 전 fleet job 취소. 반대로 release-blocking pipeline에 `fail-fast: false`로 두면 첫 실패 신호 묻힘. context에 맞게 명시. |
| `[P14]` | `needs:` graph incorrectness | MAJOR | downstream job의 `needs:` 누락 / 잘못된 의존 → upstream 실패에도 downstream 실행, 또는 race로 stale artifact 사용. |

**Pet peeve**:
- `<code>\|\| true</code>` 가 보이면 무조건 일단 CRITICAL 후보. 정당한 사유(known-flaky를 임시 격리 등) 없으면 머지 금지.
- timeout 없는 job은 daily regression이 목요일 새벽 3시에 멈춰도 누가 모름 → `[P2]` MAJOR.
- `set -euo pipefail` 빠진 bash step은 중간 실패가 묻힌다 → `[P12]`. multi-command step일수록 가시성 손실 큼.

---

## Test Determinism `[T*]`

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[T1]` | Flake-by-retry | CRITICAL | flaky test에 `@retry` / `pytest-rerunfailures` 도배. **Root cause 회피**. |
| `[T2]` | Time-based assertion | MAJOR | `time.sleep`, `datetime.now()` 비교 — FakeClock 사용. |
| `[T3]` | Network in unit test | MAJOR | unit test가 외부 호출. integration으로 격리 또는 fake 사용. |
| `[T4]` | Shared mutable fixture | MAJOR | 테스트 간 순서 의존. `pytest-randomly` 적용 시 깨짐. |
| `[T5]` | Coverage drop without note | MAJOR | 커버리지 하락 + PR 설명 없음. |
| `[T6]` | "Add tests later" promise | MAJOR | 구현만 들어오고 테스트 후속 약속. **거의 안 옴.** 같은 PR에 요구. |
| `[T7]` | Assertion without failing scenario | MINOR | `assert result is not None`만 — 의미 있는 invariant 검증 X. |
| `[T8]` | Unmarked integration test | MINOR | `-m integration` 마커 누락. unit run에 섞임. |

**Pet peeve**:
- `[T1]` retry 도배 → daily regression 신뢰도 무너뜨림. 해결책은 root cause fix 아니면 quarantine + ticket.
- `[T6]` "나중에 테스트" → 절대 안 온다. 이번 PR에서 요구.

---

## Secret & Runner Safety `[S*]`

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[S1]` | Hard-coded secret | CRITICAL | API key / token / password literal. |
| `[S2]` | Secret in step output / log | CRITICAL | `echo $TOKEN`, `set -x`로 secret이 log에 노출. |
| `[S3]` | Secret in artifact | CRITICAL | upload된 artifact에 secret 포함 (build output, dump 등). |
| `[S4]` | Missing redaction | MAJOR | log 출력에 redaction processor 미적용. |
| `[S5]` | Self-hosted runner trust assumption | MAJOR | fork PR이 self-hosted runner에서 실행되는 경로. |
| `[S6]` | Secret over-scope | MAJOR | repo-level secret이면 충분한 곳에 org-level secret 사용. |
| `[S7]` | Secret rotation absent | MINOR | 만료 정책 / rotation 절차 없음. |
| `[S8]` | env-leaked secret | MAJOR | OAuth/token이 startup 후 `os.environ`에 잔존 (`--insecure-env` 외). |

**Pet peeve**: `[S2]` — `set -x`나 `--verbose` 모드에서 secret이 노출되는 경우. CI debug 시도가 secret leak으로 직결.

---

## Observability `[O*]`

"이게 daily regression에서 꺼지면 누가, 어떻게 알지?"가 답이 안 나오면 여기.

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[O1]` | Silent error path | CRITICAL | error에 log/metric/alert 없음. 사용자가 "왜 안 되지?" 만 알 수 있음. |
| `[O2]` | Unstructured log | MAJOR | `_log.info(f"Failed for {id}")` — Loki/Grafana 쿼리 어려움. structured field로. |
| `[O3]` | Missing correlation id | MAJOR | 요청/이벤트 흐름 추적 불가. |
| `[O4]` | No metric on critical path | MAJOR | regression 통과율 / pipeline 성공률 등 핵심 KPI 미수집. |
| `[O5]` | No alert on critical path | MAJOR | 메트릭은 있으나 alert 미연결. **모니터링 ≠ 알림**. |
| `[O6]` | Log volume regression | MINOR | per-event log가 N배 증가 → 비용 / 노이즈. |
| `[O7]` | PII / secret in log | CRITICAL | redaction 미통과 필드. (`[S4]`와 함께 잡힘) |
| `[O8]` | Retention shorter than on-call window | MAJOR | artifact / CI log / Loki retention이 on-call 응답 시간보다 짧음. 새벽에 실패한 regression이 출근 전에 사라짐 → root cause 추적 불가. retention ≥ on-call rotation 주기. |

**Pet peeve**:
- `[O1]` — "에러는 났는데 silent로 swallow하고 retry" → 100번째 retry까지 아무도 모름. CRITICAL.
- `[O2]` — Loki에서 검색되지 않는 로그는 없는 거나 마찬가지.

---

## Infrastructure-as-Code `[I*]`

Terraform · K8s manifests · Helm · Ansible · GitHub Actions YAML · launchd / systemd unit.

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[I1]` | Drift between IaC and live | MAJOR | code 변경 없이 live 상태가 다름 (직접 수정). |
| `[I2]` | Non-additive migration | CRITICAL | 기존 마이그레이션 in-place 수정. linear/additive 원칙 위반. |
| `[I3]` | Missing rollback path | MAJOR | rollout만 있고 rollback 절차 없음. |
| `[I4]` | Missing health/readiness | MAJOR | K8s probe 누락 → traffic이 not-ready pod로. |
| `[I5]` | Resource limits missing | MAJOR | container `limits` 없음 → noisy neighbor. |
| `[I6]` | RBAC over-grant | MAJOR | namespace 충분한데 cluster role. |
| `[I7]` | Hard-coded environment | MAJOR | env-specific value가 manifest에 직접. Helm/values 분리. |
| `[I8]` | No diff-preview gate | MINOR | `terraform plan` 결과를 PR에 노출하지 않음. |

**Pet peeve**: `[I2]` — daily regression DB의 마이그레이션이 in-place 수정되면 환경 간 schema 불일치로 며칠 뒤 폭발.

---

## NPU Lab `[L*]`

NPU 하드웨어 fleet · 공유 자원 · regression scheduler.

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[L1]` | Missing device lock/queue | CRITICAL | 같은 NPU device를 여러 job이 동시 점유. corruption / flake. |
| `[L2]` | Device leak | MAJOR | test 종료 시 자원 해제 안 됨 (open file / mmap / shared memory). |
| `[L3]` | Lab assumption hard-coded | MAJOR | `/dev/rebel0` 같은 specific device path. fleet에서 안 통함. |
| `[L4]` | No quarantine path | MAJOR | flaky/broken device를 격리할 수단 없음 → 다음 job에서도 깨짐. |
| `[L5]` | Firmware/driver version assumption | MAJOR | 특정 FW/KMD 버전에서만 동작. version probe 없음. |
| `[L6]` | Long-running test without checkpoint | MINOR | regression test가 6시간인데 중간 진행상황/재개점 없음. |

**Pet peeve**: `[L1]` lock 누락 → 두 job이 같은 device 잡으면 둘 다 fail. retry로 가려져서 며칠씩 silent.

---

## Spec / Process Drift `[D*]`

| ID | Name | Default severity | When to flag |
|---|---|---|---|
| `[D1]` | Implementation without spec update | MAJOR | 동작이 바뀌었는데 PLAN.md / spec / RFC 미수정. |
| `[D2]` | Spec without test | MAJOR | spec에 명시된 동작인데 검증 테스트 없음. |
| `[D3]` | Verify step removal | CRITICAL | "왜 verify 날렸어?" — 검증 단계를 사유 없이 제거. |
| `[D4]` | Bypassed required review | CRITICAL | review-required path 우회 (force push / squash 후 review 손실). |
| `[D5]` | Hidden behavior change in refactor | MAJOR | "refactor" PR인데 동작 변경 동반. |
| `[D6]` | Documented runbook untouched | MINOR | 운영 절차 변경 동반인데 runbook 미갱신. |

**Pet peeve**: `[D3]` — verify를 우회하는 패턴은 daily regression의 신뢰 기반을 직접 무너뜨린다. 사유 + 대체 검증 없으면 CRITICAL.

---

## How to add a rule

리뷰 중 카탈로그에 없는 안티패턴이 두 번 이상 나오면:
1. 사용자에게 보고: `"카탈로그에 없는 패턴 X — 추가할까요?"`
2. 승인 시 적절한 카테고리에 ID 추가 (`[P12]` 형태로 다음 번호).
3. severity / when-to-flag / pet peeve 한 줄.

룰은 **자라는 카탈로그**다. 리뷰 한 번이 카탈로그 한 줄로 남도록.
