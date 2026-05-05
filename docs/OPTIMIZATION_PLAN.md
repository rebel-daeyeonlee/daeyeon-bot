# Optimization Plan — daeyeon-bot

> Companion to `docs/PLAN.md` (Phases 0–7 implementation plan). This document
> is the post-Phase-7 hardening roadmap from a Senior Engineering review of the
> live codebase as of 2026-05-05. It catalogs concrete defects and proposed
> fixes, ordered by phase. Every item carries a file:line anchor so an
> implementer can jump straight to the call site.
>
> **Out of scope** — anything that violates the daemon invariants in
> `CLAUDE.md` (multi-tenancy, message brokers, container orchestration).
> Most fixes are local refactors. A3 (rate-limit bucket) is the only
> item that touches multiple modules + a new migration; everything else
> stays within 1–3 files.

## Table of contents

| Phase | Item | Risk · Effort |
|---|---|---|
| A. Correctness | A1 Duplicate-review on 5xx after server-accepted POST | high · M |
| | A2 AuthError leaves outbox row stuck `running` | high · S |
| | A3 Rate-limit bucket is a stub | medium · L |
| | A4 `_enforce_redaction` over-DLQs on entropy false-positives | medium · M |
| | A5 `request_gen` type drift (str ⇄ int) | low · XS |
| B. Robustness | B1 `gh_review_requested` first-poll delayed 5 min | medium · XS |
| | B2 `gh_review_requested` doesn't honor PAUSE | medium · S |
| | B3 Trigger not wired into supervisor quarantine | medium · S |
| | B4 Token rotation operator workflow | low · S |
| | B5 Empty-assistant-text log lacks prompt size | low · XS |
| C. Performance | C1 `claim_one` 3 statements + 2 commits | cosmetic · M |
| | C2 N+1 `pr_get` calls per poll cycle | low · S |
| | C3 JSON parse cost on `gh --paginate` blobs | low · XS |
| D. Operability | D1a Lifecycle test coverage | medium · M |
| | D1b Ops CLI test coverage | medium · S |
| | D2 `doctor` doesn't warn on missing `config.toml` | low · XS |
| | D3 `Config(extra="allow")` masks typos | low · S |
| | D4 Heartbeat self-alert log lacks actionable hint | low · XS |
| | D5 `dedup_keys` rows accumulate (cosmetic) | cosmetic · XS |
| E. Code & docs | E1 `pr_review.handle()` is 220 lines | low · M |
| | E2 ~~moved into E5~~ | — |
| | E3 `RealClaudeSession.__aexit__` masks teardown errors | low · XS |
| | E4 `infra/schemas.py` is empty stub | cosmetic · XS |
| | E5 `CLAUDE.md` doc drift (API + coverage) | low · XS |
| | E6 PR-review handler hard-coded `concurrency=1` | low · S |
| | E7 `pr_review` audit `error` unused on skip paths | low · XS |

---

## Risk / effort legend

- **Risk** — `high` (correctness or data-loss), `medium` (operability or
  performance regression visible to operator), `low` (polish, defensive
  hardening), `cosmetic` (style/doc only).
- **Effort** — `XS` (≤1 hour), `S` (≤½ day), `M` (½–2 days), `L` (>2 days).

## Health snapshot

| Signal | Value |
|---|---|
| Tests | **287/287 pass** (`just test`) |
| Coverage | **83 %** overall — core/app meets ≥90 %, infra meets ≥80 %, **cli is well below the 60 % bar** (`cli/lifecycle.py` 31 %, `cli/ops.py` ~50 %). `CLAUDE.md` claims 77 %, drifted vs reality — fix in E5. |
| Lint | `ruff check` clean, `ruff format --check` clean |
| Type | `pyright --strict` 0 errors / 17 warnings (acceptable) |
| Live data races | None detected — `claim_one`/`settle` atomicity verified by `tests/integration/test_dispatcher_loop.py` |

**Solid:** outbox claim-row, recovery-on-boot, 2-phase shutdown, structlog
redaction (regex + Shannon-entropy fallback), supervisor quarantine for
trigger crash storms, mtime-cached persona reload, audit append/supersede
state machine, test fakes (`FakeClock`, `FakeClaudeSession`, `InMemorySecrets`).

**Not solid yet:** rate-limit bucket is a stub; `gh_review_requested` polls
have a 5-minute startup delay and don't honor PAUSE; duplicate-review risk
on a 5xx returned *after* GitHub has already accepted a POST; `pr_review.handle()`
is a 220-line state machine in one method.

---

## Phase A — Correctness fixes (must land first)

### A1. Duplicate-review on 5xx after server-accepted POST  &nbsp;`risk: high · effort: M`

**Where:** `infra/gh_cli.py:124` (`post_review`) → `_raise_error` at line 224.

**Symptom:** GitHub accepts the POST and creates the review, then a 5xx is
returned (proxy timeout, partial write). `_raise_error` maps 5xx to
`TransientError`. The dispatcher returns `Retry(DEFAULT_BACKOFF_S)`. The next
attempt re-runs stages (a)–(j) and posts a *second* review for the same
`(repo, pr, head_sha)` — there is **no client-side dedup before POST**.

The "already-reviewed" short-circuit at `handlers/pr_review.py:250` only
fires when an audit row with `status='posted'` exists, but the audit insert
at line 326 only happens *after* a successful return from `post_review`.

**Fix:** Disambiguate 5xx on POST inside `gh_cli.post_review` itself, before
the dispatcher ever sees a `TransientError`. Concretely:

1. Add `GhCli.list_reviews_at(repo, pr_number, commit_id, login)` →
   `list[dict]` (one new endpoint in `contracts/github-api-surface.md`,
   `GET /repos/{repo}/pulls/{n}/reviews?per_page=100` filtered client-side).
   The filter is `submitted_at != null AND commit_id == head_sha AND
   user.login == login` (a pending review has `submitted_at == null`,
   which excludes it). `login` is the operator's GitHub username — the
   bot posts as the operator, so reuse the same `gh.config.github_username`
   the handler already passes for the self-authored skip
   (`handlers/pr_review.py:120, 175`). Do NOT use `gh api /user` here:
   `infra/gh_cli.py:78-83` already calls that once at boot, and
   re-resolving per-POST adds a request to the very critical path we
   are trying to dedup.
2. In `post_review`, on 5xx (and only 5xx — keep 401/403/422 mapping), call
   `list_reviews_at(...)` once. If a matching review exists, return its
   payload as if the original POST succeeded. If not, *then* raise
   `TransientError` so the dispatcher retries normally.
3. **Nested-failure policy:** if `list_reviews_at` *also* fails (any
   exception), do NOT recurse — propagate the original `TransientError`
   from the POST. The handler's idempotency contract still tolerates a
   double-post if both probes fail catastrophically; correctness here is
   "best-effort suppression," not "absolute dedup."

Keeping the dedup inside the wrapper means the handler stays oblivious —
the contract `post_review` returns is "the review you just posted (or
discovered already exists)." No changes in `pr_review.py`.

**Test:** `tests/unit/test_gh_cli.py::test_post_review_5xx_dedup` — fake
the subprocess to return 502 on POST and a matching review on the
follow-up GET. Assert the wrapper returns the matching review and only
one `posted` audit row exists end-to-end.

**Out-of-scope alternative considered:** raising `PermanentError` on 5xx
POST and letting the operator replay. Rejected — turns a transient
network blip into ops toil.

---

### A2. AuthError leaves outbox row stuck `running`  &nbsp;`risk: high · effort: S`

**Where:** `app/dispatcher.py:205-211` (`_run_one` AuthError branch).

**Symptom:** When a handler raises `AuthError`, the dispatcher logs and
calls `self.stop()` — but **never calls `outbox.settle()`**. The row
remains `status='running'` in the DB. On the next boot,
`recover_interrupted_rows` (`infra/outbox.py:300`) demotes it to
`interrupted` then to `pending` (idempotent) or `dead_letter`
(non-idempotent). For an idempotent handler this means the row will be
re-claimed and re-fail with AuthError until the operator rotates the
token — wasted boot cycle every restart.

**Fix:** Bind the AuthError to a name and mark the row interrupted before
`self.stop()`:
```python
except AuthError as exc:               # currently `except AuthError:`
    _log.error("dispatcher.auth_error", outbox_id=job.outbox_id)
    await outbox.mark_interrupted(
        self.db,
        outbox_ids=[job.outbox_id],
        now=self.clock.now(),
        reason=f"AuthError: {exc}",
    )
    self.stop()
    return
```
This makes the row's state explicit so `inspect status` shows the AuthError
victim, and recovery's classification is identical (idempotent → rerun)
but the operator has the breadcrumb.

**Test:** Add `tests/unit/test_dispatcher.py::test_auth_error_marks_interrupted`
— a fake handler that raises `AuthError`, assert the outbox row's
`status='interrupted'` and `last_error` starts with `AuthError:` after
the dispatcher returns.

---

### A3. Rate-limit bucket is a stub  &nbsp;`risk: medium · effort: L`

**Where:** `app/ratelimit.py:1-9` — `take()` raises `NotImplementedError`.
Migration `001_init.sql` creates `ratelimit_buckets` but **no caller
exists**. `CONTRACTS.md §5` mandates atomic `UPDATE … SET tokens = tokens
- 1 WHERE name=? AND tokens >= 1`.

**Symptom:** The only quota guards today are (a) PAUSE flag (manual) and
(b) Anthropic's own `RateLimitError` (reactive — already over the line).
For the operator's Pro/Max plan there's no proactive throttle, so a
runaway loop (e.g. a degenerate retry) can burn through the quota
window before PAUSE is set.

**Design decision — gate where, not how:** the bucket gate must be at the
poll-loop level, *parallel to* `is_paused()` at `dispatcher.run()`. **Not**
inside `_run_one`. Reason: `_run_one`'s only failure currency is
`HandlerResult`, and both `Retry` and `DeadLetter` increment `attempt`.
Burning attempts on rate-limit-induced retries hits `MAX_TRANSIENT_ATTEMPTS
= 10` and dead-letters genuinely-fine work. Gating before `claim_one()`
keeps the row in `pending` and never increments anything.

**Fix:**
1. Implement `take(conn, bucket)` as a single atomic UPDATE per `CONTRACTS
   §5`. **Signature change vs. today:** the stub at `app/ratelimit.py:1-9`
   is `take(bucket: str) -> bool`; the new contract is
   `take(conn: aiosqlite.Connection, bucket: str) -> bool`. No
   non-test caller exists today, so the change is purely additive.
   Refill is time-based, computed in SQLite arithmetic so the SQL is
   self-contained. Sketch (illustrative — implementer must verify
   timestamp-format compatibility; SQLite `julianday` accepts ISO-8601
   with `Z` or `±HH:MM` since 3.42, but the codebase uses
   `datetime.isoformat()` which emits `+00:00` — confirm the SQLite
   bundled with the target Python before shipping):
   ```sql
   UPDATE ratelimit_buckets
      SET tokens = MIN(
            capacity,
            tokens + ((julianday(?) - julianday(last_refill_at)) * 86400.0 * rate_per_s)
          ) - 1,
          last_refill_at = ?
    WHERE name = ?
      AND tokens + ((julianday(?) - julianday(last_refill_at)) * 86400.0 * rate_per_s) >= 1
   ```
   Parameter binding order: `(now_iso, now_iso, bucket_name, now_iso)` —
   four `?` placeholders, three of them carry `now`. Returns `True` iff
   `cursor.rowcount == 1`. *Alternative timestamp strategy:* store
   `last_refill_unix REAL` (Unix epoch seconds) instead of ISO text.
   Eliminates the `julianday` parse risk and removes one `?`.
2. In `dispatcher.run()`, after the `is_paused()` check (line 82), add:
   ```python
   if not await ratelimit.take(self.db, "claude_call"):
       await self._wait_for_stop_or_tick()
       continue
   ```
3. Seed `ratelimit_buckets` rows in migration `003_*.sql` with sane defaults
   (e.g. claude_call: capacity=60, rate=1.0/s — a soft per-minute cap).
   Knobs in `[ratelimit]` config override these at boot via UPSERT.
4. Add `inspect ratelimit` CLI subcommand to print current token state and
   refill rate.

**YAGNI for now:** a separate `gh_api` bucket. `gh_cli.py` already
classifies 403+rate-headers as `RateLimitError` (line 217) and the
dispatcher already does an extra-long backoff on it. The bucket is
only valuable for the Claude API where we *don't* see headers ahead of
time.

**Test:** New `tests/unit/test_ratelimit.py` with concurrent `take` calls
against a real `aiosqlite` (10 parallel coroutines, capacity=5 → exactly
5 should succeed). One integration test that spins the dispatcher with
the bucket exhausted and asserts no rows are claimed until refill.

---

### A4. `_enforce_redaction` PermanentError on legitimate output  &nbsp;`risk: medium · effort: M`

**Where:** `handlers/pr_review.py:520-528`. Calls `redact_text` with the
Shannon-entropy fallback (≥4.5 bits/char on ≥24-char strings) → any
high-entropy string in Claude's review summary (a long base64 ID, a
hash, even a code snippet flagged as "looks like a key but isn't") raises
`PermanentError` and the row goes to DLQ.

**Symptom:** Real review content can trip the entropy heuristic without
containing an actual secret. Operator wakes up to a DLQ'd PR with
`reason="redaction would alter posted content (summary)"` and no
indication of *what* was flagged.

**Fix:** Split the redaction signal into two confidence tiers and act
differently per tier — but **only at the post-to-GitHub site**. The
log-sink redaction processor (`infra/logging.py`) MUST keep its current
strict behavior (always redact entropy hits), because logs land in
journald / launchd-stderr where any leak is permanent. We are loosening
the policy for *posted PR-review content*, not for logs.

1. Add a sibling `redact_with_provenance(text)` in `infra/logging.py`
   that returns `(redacted_text, [(start, end, reason)])` where
   `reason ∈ {"slack", "aws", "jwt", "anthropic", "gh", "entropy"}`.
   The existing `redact_text` keeps its current signature so the
   structlog processor stays unchanged.
2. In `_enforce_redaction`:
   - **Named-token hit** (`reason != "entropy"`) — keep current behavior:
     raise `PermanentError`. That's a real secret leaking from the model
     and we must not post it.
   - **Entropy-only hit** — log `_log.warning("pr_review.redaction_entropy",
     spans=[...])` and post the **original** (unmodified) text. Rationale:
     the entropy heuristic exists to catch unknown-format secrets, but its
     false-positive rate on natural review prose (hashes, identifiers,
     code) is too high to gate posts on. Logged spans give the operator
     evidence to triage post-hoc.
3. Annotate the audit row's `error` column with the matched span(s) on
   the named-token-refusal path so the operator can see *what* was caught
   without grepping logs.

**Test:** `tests/unit/test_pr_review_redaction.py` — three cases:
(a) named-token in summary (e.g. `xoxb-` Slack prefix) → PermanentError +
audit shows the span,
(b) entropy-only hit (use `secrets.token_urlsafe(24)` to generate a
genuinely high-entropy string ≥4.5 bits/char) → review posted unchanged +
warning log emitted,
(c) clean prose → no warnings, no spans, posted normally.

---

### A5. `request_gen` is a string in payload, int in DB  &nbsp;`risk: low · effort: XS`

**Where:** `triggers/gh_review_requested.py:229` writes `str(request_gen)`
into the payload. `handlers/pr_review.py:403` reads it back as a string
and never converts to int. The audit row's `request_gen` column is INT in
the schema but the handler passes the string through `audit_kwargs` at
line 169 — actually persisted via `pr_review_audit.insert_audit` which
accepts `Any`.

**Symptom:** `inspect audit` reports `request_gen='1'` (string). Joins
against `gh_review_requested_state.request_gen` (int) compare-by-cast
work in SQLite but break in any operator query that does
`WHERE request_gen = ?`.

**Fix:** Trigger emits `int`. Handler parses as `int`. Audit row stays
INT. Single line per call site.

**Test:** Existing `tests/integration/test_gh_review_requested_loop.py`
should already exercise this — add an explicit type assertion.

---

## Phase B — Robustness / at-least-once edge cases

### B1. `gh_review_requested` first-poll is delayed 5 min  &nbsp;`risk: medium · effort: XS`

**Where:** `triggers/gh_review_requested.py:80` — `await asyncio.sleep(...)`
runs **before** `poll_once()`. With the default `poll_interval_seconds=300`,
the operator sees nothing happen for 5 minutes after restart.

**Fix:** Run `poll_once()` first, then sleep. Restructure preserves the
existing error-mapping branches:

```python
async def run(self, emit, ctx):
    del emit, ctx
    while True:
        try:
            emitted = await self.poll_once()
        except AuthError:
            raise
        except RateLimitError:
            _log.warning("gh_review_requested.rate_limited")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(self.poll_interval_seconds)
        except (TransientError, PermanentError) as exc:
            _log.warning("gh_review_requested.poll_failed", error=str(exc))
        else:
            if emitted:
                _log.info("gh_review_requested.emitted", count=emitted)
        await asyncio.sleep(self.poll_interval_seconds)
```

**Test:** Existing trigger tests need to assert the first call to
`poll_once` happens before the first `sleep`.

---

### B2. `gh_review_requested` doesn't honor PAUSE  &nbsp;`risk: medium · effort: S`

**Where:** `triggers/gh_review_requested.py:76-94` — the trigger ignores
the PAUSE flag entirely. PAUSE is checked only in the dispatcher (via
`is_paused`) and in `pr_review.handle()` (via `pause_guard`).

**Symptom:** With PAUSE up, no events fire (handler short-circuits) but
the trigger still hits `/search/issues` + N×`/repos/.../pulls/{n}` every
5 minutes — wasting GitHub API quota during a paused window.

**Fix:** Use the dispatcher-style sync check `pause.is_paused(config.pause_flag_path)`
at the top of each iteration. **Not** the handler's async `PauseGuard`
(`Callable[[], Awaitable[None]]`) — that primitive raises `QuotaError`
to short-circuit a handler invocation, which is the wrong contract for
a long-running poller. Inject `pause_flag_path` (or a pre-bound
`Callable[[], bool]`) via the trigger constructor; on PAUSE, sleep one
interval and continue without the API calls.

**Test:** `tests/unit/test_gh_review_requested.py` — add a paused-state
fixture that flips on/off; assert `gh.search_review_requested` is not
called while paused, and is called once PAUSE is cleared.

---

### B3. Trigger not wired into supervisor quarantine  &nbsp;`risk: medium · effort: S`

**Where:** `app/lifecycle.py:181-214` (`_supervised_trigger`) wraps the
trigger task but does **not** integrate with `app/supervisor.py`'s
`FailureWindow` / quarantine tables. A buggy trigger that raises
`PermanentError` every cycle just logs at `warning` and continues.

**Fix:** Use `TriggerSupervisor` (already implemented but unused for
`gh_review_requested`). After 5 failures inside a 10-min window, write a
`trigger_quarantine` row and stop the trigger task. CLI `ops` already has
release commands.

**Test:** `tests/integration/test_supervisor_quarantine.py` — extend with
the gh trigger.

---

### B4. Token rotation operator workflow  &nbsp;`risk: low · effort: S`

**Where:** `scripts/setup-token.sh` (the actual `just setup-token` target —
there is no `cli/setup.py` today; the script wraps `keyring set`
directly) and the operator runbook. Today's flow on rotation: run the
script → `launchctl kickstart -k gui/$(id -u)/com.rebellions.daeyeon-bot`
(or `systemctl restart`). Two manual steps, easy to do one without the
other and end up with a running daemon on the old token until next
AuthError.

**Considered & rejected:** plumbing per-session token re-resolve into
`make_real_factory` — adds retry-loop complexity to handle the race
between an in-flight SDK call and a rotation, and makes the AuthError
exit-78 contract murky. Single-operator daemon's restart cost is <30s.

**Fix:** Extend `scripts/setup-token.sh` (or add a sibling
`scripts/rotate-token.sh`) with a `--rotate` flag that runs `keyring set`
followed by the platform restart command, with rollback (re-set the
previous token) on restart failure. Update `just setup-token` and the
runbook to document the rotation path. No daemon-side changes; no new
Python module.

**Test:** Bash-level smoke test under `tests/scripts/` (or document a
manual rotation drill in the runbook) — pure-Python unit tests are not
the right tool here. If a future refactor moves token management into a
Python CLI module, gate that work behind its own item rather than
expanding scope here.

---

### B5. Empty-assistant-text log lacks prompt size  &nbsp;`risk: low · effort: XS`

**Where:** `infra/claude.py:226-228` raises `TransientError("claude returned
no assistant text")`. The retry path is bounded by `MAX_TRANSIENT_ATTEMPTS
= 10` (`app/dispatcher.py:47`), so the symptom is contained — but when
debugging *why* the model returned empty, the operator has no breadcrumb.

**Fix:** Log `prompt_chars=len(prompt)` at `_log.warning` immediately
before the `raise TransientError`. One line.

**Test:** Existing `tests/unit/test_claude.py` should already exercise
the empty-text path — assert the warning event includes `prompt_chars`.

---

## Phase C — Performance

### C1. `claim_one` performs 3 SQL statements + 2 commits per row  &nbsp;`risk: cosmetic · effort: M`

**Where:** `infra/outbox.py:97-168`. SELECT → UPDATE+commit → SELECT events+commit.
At 0.5 s poll on local-disk WAL, each claim is sub-millisecond — so this
is **not in the active sprint plan**. Documenting only because the
optimization is real and worth picking up if poll latency ever surfaces
in profiling.

If addressed: switch to a single `UPDATE … RETURNING` (aiosqlite ≥0.20
supports it) for the claim, fold the commit into the same transaction as
the events read. Re-test against `test_claim_one_atomic`.

---

### C2. N+1 `pr_get` calls per poll cycle  &nbsp;`risk: low · effort: S`

**Where:** `triggers/gh_review_requested.py:107` — for every entry in the
search result, `_fetch_head_shas` (line 145) spawns one `pr_get`
subprocess. With 20 review-requested PRs that's 21 `gh` invocations per
cycle (1 search + 20 metadata) every 5 minutes.

The current behavior is *correct* — search results don't include head
SHA, so `pr_get` is the only way to disambiguate cases (1)/(2) of the
state-machine §5 (new SHA vs same SHA new gen). But for PRs already in
the state table whose recorded `head_sha` matches a freshly-cached
hint, the call is wasted.

**Fix:** GitHub's `/search/issues` exposes `pull_request.url` but not
`head.sha`. However it does include `updated_at`. Skip `pr_get` for any
`(repo, pr)` already in the state table whose `last_observed_at >=
updated_at` from the search payload — same SHA, no reviewer churn since
last cycle. Cuts N-1 calls per cycle for the steady-state case.

**Test:** `tests/unit/test_gh_review_requested.py` — add a fake `gh` whose
search returns a PR with `updated_at` older than the state's
`last_observed_at`. Assert `pr_get` is NOT called for that PR.

---

### C3. JSON parse cost on `gh --paginate` blobs  &nbsp;`risk: low · effort: XS`

**Where:** `infra/gh_cli.py:273-302` — `_split_json_chunks` is a
character-by-character state machine. Fine for small payloads, but for
`pr_files` on a 50-file PR with 50 KB+ patches it's ~10 ms of pure Python.
`json.JSONDecoder.raw_decode` does the same in C in microseconds.

**Fix:**
```python
decoder = json.JSONDecoder()
chunks = []
i = 0
while i < len(text):
    obj, end = decoder.raw_decode(text, i)
    chunks.append(obj)
    i = end
    while i < len(text) and text[i].isspace():
        i += 1
```

**Test:** Existing pagination tests still pass; add a benchmark
(`tests/unit/test_gh_cli.py` — micro-bench just for the file).

---

## Phase D — Operability

### D1a. `cli/lifecycle.py` coverage is 31 %  &nbsp;`risk: medium · effort: M`

**Where:** `tests/unit/test_cli_*.py`. PLAN §6.3 sets the floor at 60 %
for cli/. Most paths require a real launchd / systemd to exercise so
they're naturally hard to cover, but the in-process flow (boot → stop
event → drain) is testable with `BootOptions(external_stop_event=...)`.

**Fix:** Add `tests/integration/test_lifecycle_boot.py` that:
1. Builds an `InMemorySecrets` + `FakeClaudeSession` config.
2. Sets `external_stop_event` after one tick.
3. Asserts: pidfile released, WAL checkpointed, exit clean.
4. Repeats for AuthError → exit 78 path.

Target: `cli/lifecycle.py` to ≥60 %.

---

### D1b. `cli/ops.py` coverage is ~50 %  &nbsp;`risk: medium · effort: S`

**Where:** `tests/unit/test_cli_ops.py`. The replay/release/quarantine-list
commands are partially covered. Gaps are mostly the printing paths and
the confirmation gates.

**Fix:** Extend the existing test file with:
- `ops replay <id>` without `--confirm` — assert it prints the dry-run
  preview and DOES NOT mutate the row.
- `ops replay <id> --confirm` — assert the row transitions and emits the
  expected log line.
- `ops list-quarantine --json` — assert JSON shape.

Target: `cli/ops.py` to ≥60 %.

---

### D2. `doctor` doesn't warn on missing `config.toml`  &nbsp;`risk: low · effort: XS`

**Where:** `cli/ops.py` (the `doctor` subcommand under `daeyeon-bot ops doctor`).
When neither `config.toml` nor
`DAEYEON_BOT_CONFIG` is set, `Config()` falls back to defaults silently.
A first-time operator gets confusing behavior (e.g., default state_dir
in cwd) without a hint.

**Fix:** In `doctor`, after `load(config_path)`, log the resolved config
path or "using defaults (no config.toml found)". One line.

---

### D3. `Config(extra="allow")` masks typos  &nbsp;`risk: low · effort: S`

**Where:** `app/config.py:117`. The top-level `Config` accepts unknown
TOML keys silently. A typo like `[handlrs.pr_review]` becomes a
no-op section instead of a startup error.

**Fix:** Tighten the top-level to `extra="forbid"`. The two `extra="allow"`
on `TriggerEntry` / `HandlerEntry` are intentional (pass-through to
constructors) and should stay.

**Test:** `tests/unit/test_config.py::test_typo_in_section_rejected`.

---

### D4. Heartbeat self-alert log lacks actionable hint  &nbsp;`risk: low · effort: XS`

**Where:** `app/heartbeat.py` `_log.error("heartbeat.tick_lag", elapsed_s=…)`.
Operator sees the line but no guidance. Add `hint="check journald for
blocking operations; consider raising tick_s or reducing concurrency"`
to the structured event so it shows up in JSON sinks.

---

### D5. Unused `dedup_keys` rows accumulate without prune  &nbsp;`risk: cosmetic · effort: XS`

**Where:** `app/prune.py` covers `events`, `outbox`, `runs` by retention
keep counts. `dedup_keys.expires_at` is set but never DELETE'd — TTL is
respected at lookup time (`is_deduped`) but rows persist.

For one operator at ~1 PR review/day with 1-day TTL, steady-state size
is ~1 row. Even a year of activity is hundreds of rows. **Not a real
problem** — including only for completeness because `prune.py` already
has the seam (`prune_table` helper). Add a one-line `DELETE FROM
dedup_keys WHERE expires_at < ?` if it's ever profiled as relevant.

---

## Phase E — Code structure & docs

### E1. `pr_review.handle()` is 220 lines, PLR0915 silenced  &nbsp;`risk: low · effort: M`

**Where:** `handlers/pr_review.py:124-345`. Decompose into pipeline
stages without changing the order:

```python
async def handle(self, event, ctx):
    await self.pause_guard()
    state = await self._prepare(event, ctx)        # (a)+(b) parse + persona + pr_get
    if state.skip_self_authored or state.skip_withdrawn:
        return await self._record_skip(state)       # (c)+(d)
    if state.over_budget:
        await self.pause_guard()
        return await self._post_too_large(state)    # (e)
    if state.already_reviewed:
        return await self._record_already(state)    # (f)
    await self.pause_guard()
    review = await self._run_claude(state, ctx)     # (g)
    await self.pause_guard()
    return await self._post_review(state, review)   # (h)..(k)
```

Each method returns either `HandlerResult` or a hand-off `_State` dataclass.
**Pause-guard call sites are intentional** — preserve all four (top, before
too-large post, before Claude, before final post) since they are part of
the operator-visible contract: PAUSE during a long-running handler still
short-circuits before the next external side-effect. Reduces cognitive
load and unlocks per-stage unit tests.

**Test:** Existing handler tests cover the end-to-end paths; add per-method
tests after the split.

---

### E2. *(merged into E5)*

Was "CLAUDE.md references obsolete API" — folded into E5 which covers
the same drift plus the coverage-percentage staleness. Section number
preserved to keep the table-of-contents anchors stable.

---

### E3. `RealClaudeSession.__aexit__` masks unexpected exceptions  &nbsp;`risk: low · effort: XS`

**Where:** `infra/claude.py:155-158`. `try: client.disconnect() except
CLIConnectionError: return`. Other exceptions during disconnect (e.g.
unexpected `RuntimeError` from the SDK) propagate out of `__aexit__`,
masking the **original** exception that was unwinding the `async with`
(per Python's contextmanager rules). For a teardown path this should be
best-effort with broad suppression and a warning log.

**Fix:**
```python
try:
    await client.disconnect()
except Exception as exc:  # pragma: no cover — best-effort teardown
    _log.warning("claude.disconnect_failed", error=str(exc))
```

---

### E4. `infra/schemas.py` is an empty stub  &nbsp;`risk: cosmetic · effort: XS`

**Where:** `src/daeyeon_bot/infra/schemas.py`. File exists with no
contents (Phase 0 placeholder for event schema validation that landed
elsewhere in `core/events.py` and `handlers/pr_review_schemas.py`).

**Fix:** Delete the file. Update any import scan in `tests/` to confirm
no stale `from daeyeon_bot.infra import schemas` exists.

---

### E5. `CLAUDE.md` doc drift  &nbsp;`risk: low · effort: XS`

**Where:** Two staleness sites in `CLAUDE.md`:

1. "Add a new trigger" recipe references
   `infra/outbox.write_event_and_outbox_rows`. Actual public API is
   `outbox.insert_event` + `outbox.enqueue_handler` (see
   `infra/outbox.py:47, 78`).
2. Coverage line says "Current overall: 77 %". Actual is **83 %** as of
   this review (PR #d640ec0 raised coverage above the floor).

**Fix:** Search-and-replace both. Run a grep across `docs/`:

```bash
rg -n 'write_event_and_outbox_rows|Current overall: 77' CLAUDE.md docs/
```

---

### E6. PR-review handler is hard-coded `concurrency=1`  &nbsp;`risk: low · effort: S`

**Where:** `handlers/pr_review.py:65-72` — `MANIFEST.concurrency=1`.

**Trade-off:** For an operator getting 5 review-requested PRs at once
(e.g. coming back from PTO), the dispatcher serializes them. Total wall
time ≈ N × (Claude call + GitHub posts). Per-PR Claude latency is
typically 30–60 s, so 5 PRs is ~5 minutes serialized.

The Claude SDK itself is single-concurrent (one `ClaudeSDKClient` per
session), but `gh` calls and DB writes have no such constraint. With
`concurrency=2` the second PR's prep work (pr_get, pr_files) overlaps
with the first PR's Claude wait.

**Fix:** Bump default `concurrency` to 2 in the manifest, gated behind a
config knob in `[handlers.pr_review]` (`concurrency = 2`, default 1 to
preserve current behavior on rollout). Add an integration test that
confirms two `pr_review.handle()` calls can be in-flight simultaneously
under `concurrency=2`.

**Risk:** doubles per-cycle GitHub API pressure during fan-out; the
operator's `gh` token shares a 5000/hr quota with everything else. Keep
the default conservative.

---

### E7. `pr_review` audit `error` column unused on skip paths  &nbsp;`risk: low · effort: XS`

**Where:** `handlers/pr_review.py:178, 197, 255` — skip audits don't
populate `error`. Operator inspecting `pr_review_audit` can't tell
*why* a skip happened without cross-referencing logs. Add one-line
human-readable reason:

- `skipped_self_authored` → `error="author_login == github_username"`
- `skipped_withdrawn` → `error=f"state={pr_state}, requested={tuple_or_empty}"`
- `skipped_already_reviewed` → `error=f"prior_review_id={prior.review_id}"`

Audit table is append-mostly; this is purely additive metadata.

---

## Suggested execution order

Effort estimates assume one engineer working full-time and include test
authoring + plan/contracts doc updates. Calendar time will be longer.

| Sprint | Effort | Items | Goal |
|---|---|---|---|
| **1 — quick wins** | ~1.5–2 days | A2, A5, B1, B5, D2, D4, E3, E4, E5, E7 | XS items + low-risk small refactors. Fixes operator-visible papercuts and doc drift. (E2 was deduped into E5.) |
| **2 — correctness** | ~3 days | A1, A4, B2, B3, D3, B4 | Duplicate-review dedup, redaction provenance, trigger PAUSE/quarantine integration, config typo guard, rotate-token shell-script flag. Two M items (A1, A4) plus four S items pushes this past the previous 2-day estimate; A1 in particular needs a contracts/ doc update for the new GET endpoint. |
| **3 — quota & ops surface** | ~4–5 days | A3 (rate-limit bucket), C2, D1a, D1b, E6 | Implement and integrate the §5 atomic bucket, cut N+1 polls, raise CLI coverage to ≥60 %, optional concurrency bump. |
| **4 — perf polish** | ~1 day | C3, E1 | JSON parse hot-path + decompose `pr_review.handle()`. C1 deferred unless profiled. |

Total ≈ **9.5–11 engineering days**. A1/A2/A3 are the load-bearing
correctness fixes — they gate everything else. A3 alone is `L` (>2 days
including the new migration, dispatcher integration, CLI subcommand and
concurrency tests).

## Verification gates

Each sprint must pass before the next:

- `just check` (lint + typecheck + test) green.
- `tests/integration/` paths exercising the changed surface added/updated.
- `docs/PLAN.md` and `CONTRACTS.md` updated for **any** behavior change
  (mandatory per `CLAUDE.md`'s "Change recipes").
- Coverage delta inspected — core/app must stay ≥90 %, infra ≥80 %, cli
  trending ≥60 %.

### Sprint-specific deliverables

**Sprint 1:** every fix lands with at least one assertion in an existing
test file (no XS item should require a new file). Ends with a single
"Sprint 1" commit per item, conventional-commits format.

**Sprint 2:** `tests/unit/test_gh_cli.py::test_post_review_5xx_dedup`
and `tests/unit/test_pr_review_redaction.py` are new files. A1 also
ships an additive change to `specs/001-github-pr-review-bot/contracts/
github-api-surface.md` documenting the 6th endpoint
(`GET /repos/{repo}/pulls/{n}/reviews`).

**Sprint 3:** new migration `003_ratelimit_seed.sql` (additive). New
config knobs in `[ratelimit]`. CLI `inspect ratelimit` subcommand.

**Sprint 4:** no new public API. `pr_review.handle()` end-to-end test
suite must remain unchanged in behavior — only structure changes.

## Non-goals

- Multi-process, multi-tenant, or HA architectures.
- Replacing SQLite with a hosted DB.
- Replacing the outbox pattern with an external broker.
- Adding a web UI / OpenTelemetry exporter / Sentry.

These all violate the daemon scope set in `CLAUDE.md` ("What this is").
If any of them ever become real requirements, `docs/PLAN.md` must be
updated first; this document does not authorize them.
