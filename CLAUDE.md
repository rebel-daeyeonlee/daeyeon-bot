# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and other editors working in this
repository. Read this first; then `docs/PLAN.md` and `CONTRACTS.md` before
touching anything load-bearing.

## What this is

Personal Claude bot **daemon** for one operator (daeyeon.lee@rebellions.ai).
24/7 single-process. Wakes on triggers (manual / cron / webhook / file
watch / Slack), dispatches each event to a handler, and calls Claude on
the operator's Pro/Max OAuth subscription. **Not SaaS, not multi-tenant,
not multi-process.** Treat any change that introduces multi-tenancy,
API-key auth, message brokers, or container orchestration as out of scope
unless `docs/PLAN.md` is updated first.

### Current state — what's actually built

Phases 0–7 of `docs/PLAN.md` are landed:

| Phase | What |
|---|---|
| 0 | Scaffolding (typed config, structlog, container, registry). |
| 1 | Vertical slice: `manual` trigger → outbox → `echo` handler. |
| 2 | Reliability: pidfile + flock, recovery of `interrupted` rows, 2-phase shutdown, supervisor with quarantine. |
| 3 | Operability: PAUSE kill-switch, heartbeat task, ops/inspect/dev CLI. |
| 4 | Real Claude SDK + secrets (Keychain / 0600 file / env), log redaction, `AuthError` → exit 78. |
| 5 | Deployment: launchd plist + entrypoint, systemd unit (Type=notify), install scripts, setup-token. |
| 6 | Hardening: events retention with FK-aware cascade, hot SQLite backup, heartbeat self-alert, runbook. |
| 7 | GitHub PR-review bot (feature 001) — `gh_review_requested` polling trigger + `pr_review` handler. Lands behind `[handlers.pr_review].enabled = false`; flip to enable. Persona reloaded from `~/.claude/skills/pr-reviewer/SKILL.md` on every event by mtime. Migration 002 adds `gh_review_requested_state` + `pr_review_audit`. Auth flows through the operator's local `gh` CLI. |

Built-in triggers: `manual`, `gh_review_requested`. Built-in handlers:
`echo`, `pr_review`. Other workloads (cron, webhook, slack, digest, …) are
added one trigger/handler at a time using the recipes below.

## Daily commands

The single source of truth is `justfile`:

```bash
just sync              # uv sync --all-extras --dev
just lint              # ruff check + ruff format --check (src + tests)
just format            # ruff format + ruff check --fix
just typecheck         # pyright (strict mode)
just test              # pytest with coverage; passes args through
just test-unit         # only tests/unit
just test-integration  # only tests marked `integration`
just check             # lint + typecheck + test  (pre-commit aggregate)
just run               # daeyeon-bot run (foreground daemon)
just doctor            # daeyeon-bot ops doctor
just status            # daeyeon-bot inspect status
just migrate           # apply DDL migrations
just backup            # hot SQLite snapshot + prune to backup_keep
just prune             # apply retention defaults
just install-mac       # register launchd agent
just setup-token       # paste OAuth token → macOS Keychain
```

Single test: `uv run pytest tests/unit/test_outbox.py::test_claim_one_atomic -x`.
Integration tests need `-m integration` and use real `aiosqlite` against
tmp paths — they don't hit the network.

`uv` is the package manager (not pip/poetry). `pyright` runs in **strict**
mode (`pyproject.toml`) — type errors block merge.

## Architecture invariants

These are stable interfaces. Changes require an explicit migration plan
and an update to `docs/PLAN.md` / `CONTRACTS.md` in the same commit.

### Module layering (one-way deps)

```
core/      pure domain — dataclasses, protocols, errors. Stdlib only.
infra/     adapters — sqlite, sdk, secrets, logging. Depends on core.
triggers/  emit Event via core.protocols.Trigger.
handlers/  consume Event via core.protocols.Handler.
app/       composition — container, registry, dispatcher, lifecycle,
           supervisor, lock, heartbeat, prune, backup, replay.
cli/       Typer entry points — main, lifecycle, ops, inspect, dev.
           No business logic.
```

Banned-imports (`tool.ruff.lint TID`) is on. A cross-layer import that
violates this layering fails lint.

### One event, one cycle

```
trigger.emit_one()
   └── infra/outbox.insert_event() + enqueue_handler()      [single tx]
            ↓
dispatcher poll loop (every ~200 ms)
   └── infra/outbox.claim_one()                             [atomic UPDATE]
            ↓
   ── handler.handle(event) → HandlerResult
            ↓
   └── infra/outbox.settle(row_id, result)                  [single tx]
            └── if Ack and idempotent: insert dedup_keys
```

Every step is one SQL transaction. **Do not introduce read-modify-write
patterns in app code** — the storage layer's atomic UPDATEs are the
correctness boundary.

### Boot order is fixed

`app/lifecycle.py:boot()` follows the order in `PLAN.md` §2.3 and the
file's docstring. **Do not reorder steps.** In particular:

1. Load config + apply env overrides
2. Configure structlog (incl. redaction processor)
3. Acquire pidfile + flock
4. Open `state.db` with the standard PRAGMAs
5. Apply pending migrations
6. Probe secrets provider (token must be readable)
7. Build container (claude session factory, registry, dispatcher)
8. Start heartbeat task
9. **`outbox.recover_interrupted_rows()`** — MUST run after migrations,
   before the dispatcher poll loop. Crashed-mid-flight rows must be
   classified before a poller can re-claim them.
10. Start dispatcher + signal handlers

A change that affects boot order or shutdown phases must update both
`lifecycle.py`'s docstring and `docs/PLAN.md` §2.3/§2.4 in the same commit.

### 2-phase shutdown (180 s budget)

`app/lifecycle.py` orchestrates **Phase A** (stop claiming) → **Phase B**
(drain in-flight, `PHASE_B_BUDGET_S = 120`s) → **Phase C** (WAL
checkpoint, release pidfile lock). The dispatcher exposes
`request_stop_claiming()` / `drain(timeout)` / `stop()` — that triplet is
the contract. Tests live in `tests/integration/test_two_phase_shutdown.py`.

### At-least-once delivery + idempotency

Every handler MUST tolerate being invoked more than once for the same
`Event`. The dispatcher writes a `dedup_keys` row keyed on
`(event_id, handler, attempt_epoch)` only after `Ack` from an
`idempotent=True` handler. A non-idempotent handler that ends `interrupted`
goes to `dead_letter` — operator must `ops replay --confirm`. See
`CONTRACTS.md` §1–2.

### Outbox claim-row pattern

`infra/outbox.py:claim_one()` is the only sanctioned way to mark a row
`running`. It uses `UPDATE … WHERE claimed_by IS NULL` for race safety.
Do not bypass it — even in tests, prefer `claim_one` so race conditions
are exercised.

### HandlerResult is a sum type

```
HandlerResult = Ack | Retry(after_s) | DeadLetter(reason)
```

Defined in `core/results.py`. Exception → result mapping is centralized
in `app/dispatcher.py:_run_one`:

| Raised | Becomes |
|---|---|
| `RateLimitError` | `Retry(RATE_LIMIT_BACKOFF_S)` |
| `TransientError` | `Retry(DEFAULT_BACKOFF_S)` |
| `AuthError` | dispatcher halts (`stop()`); CLI exits **78** so the supervisor refuses to restart |
| `PermanentError` / unclassified | `DeadLetter(repr(exc))` |

Do not catch and translate exceptions inside handlers — return
`Retry`/`DeadLetter` directly or raise the typed error from `core.errors`.

### SQLite contract

- All connections must apply
  `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=5000; foreign_keys=ON`.
  `infra/storage.py:open_db` enforces this; never construct an
  `aiosqlite.connect()` directly.
- DDL lives in `src/daeyeon_bot/infra/db/migrations/NNN_*.sql`.
  **Linear, additive, never rewritten in place.** `meta.schema_version`
  is the only source of truth.
- `events.id` is UUIDv7 (sortable). `outbox.id` is auto-increment.
- Rate-limit token decrement must be a single atomic `UPDATE`
  (`CONTRACTS.md` §5). Read-modify-write in app code is wrong.

### Secrets discipline

- OAuth token never goes through `os.environ` after startup.
- Provider order: Keychain (macOS) → 0600 file (Linux) → env (only with
  the `--insecure-env` CLI flag).
- `infra/logging.py` ships a structlog redaction processor that
  scrubs Slack, AWS, JWT, Anthropic OAuth, GitHub PAT patterns plus a
  Shannon-entropy fallback (≥4.5 bits/char on ≥24-char strings). It runs
  before any sink, so handler logs of Claude payloads are safe by default.

### Heartbeat self-alert

`app/heartbeat.py:run_until_stopped()` measures wall-clock elapsed time
between ticks. If a tick wakes up later than `tick_s * STALE_FACTOR` (3×),
it emits `_log.error("heartbeat.tick_lag", elapsed_s=…)`. journald (Linux)
and launchd-stderr (macOS) surface that line directly so a hung daemon
flags itself.

## Configuration model

- `config.toml` is **not committed** (`.gitignore`). `config.example.toml`
  is the reference; copy it.
- pydantic-settings env override prefix: `DAEYEON_BOT__`, nested delimiter
  `__` (e.g. `DAEYEON_BOT__LOGGING__LEVEL=DEBUG`).
- `Config.routing[event_type] = [handler_name, ...]` is the only routing
  table — no decorator-based discovery.
- New handlers register through the explicit `if name == ...` block in
  `app/registry.py:instantiate_handler`. This is intentional; do not
  introduce entry-points or import-time side effects.

## Testing patterns

- `tests/fakes/` is a real package (`FakeClock`, `FakeClaudeSession`,
  `InMemorySecrets`, …). Reach for fakes before mocks.
- Integration tests rely on the real `aiosqlite` + migrations stack with
  `tmp_path` DBs; they're fast enough not to need mocking.
- `pytest-asyncio` mode is `auto` — async tests don't need
  `@pytest.mark.asyncio`.
- Coverage targets (`PLAN.md` §6.3): core/app ≥ 90 %, infra ≥ 80 %, cli ≥ 60 %.
  Current overall: 83 % (cli/lifecycle.py and cli/ops.py still drag the cli
  bucket below 60 % on paths that need a real launchd / systemd to exercise —
  see `docs/OPTIMIZATION_PLAN.md` D1a/D1b).

## Runtime layout

State directory: `~/.daeyeon-bot/` (config knob `runtime.state_dir`).

| File | Purpose |
|---|---|
| `state.db` (+ `-wal`, `-shm`) | SQLite WAL primary store. |
| `daeyeon-bot.pid` | Pidfile + flock — single-instance enforcement. |
| `heartbeat` | Touched every `tick_s`; mtime is the liveness signal. |
| `PAUSE` | Operator kill-switch — presence blocks Claude calls before rate-limit check. |
| `backups/state-<UTC>.db` | Snapshots from `just backup`, pruned to `retention.backup_keep`. |
| `launchd.{out,err}.log` (Mac) | Daemon stdout/stderr (structlog JSON). |

Exit codes the CLI returns:

- **0** — clean shutdown.
- **75** (EX_TEMPFAIL) — another instance holds the pidfile lock.
- **78** (EX_CONFIG) — `AuthError` or `ConfigError`; supervisors must NOT
  auto-restart (`RestartPreventExitStatus=78` on systemd; the launchd
  wrapper script enforces the same via `ThrottleInterval`).

## Change recipes

### Add a new handler

1. `src/daeyeon_bot/handlers/<name>.py`:
   ```python
   from daeyeon_bot.core.manifest import HandlerManifest
   from daeyeon_bot.core.protocols import Handler
   from daeyeon_bot.core.results import Ack, HandlerResult
   from datetime import timedelta

   MANIFEST = HandlerManifest(
       name="<name>", idempotent=True,
       dedup_ttl=timedelta(days=1),
       side_effect_key=None,
       concurrency=1,
       accepts=["<event.type>"],
   )

   class <Name>Handler(Handler):
       async def handle(self, event, *, claude) -> HandlerResult: ...
   ```
2. Register it in `app/registry.py:instantiate_handler` (`if name == "<name>": return <Name>Handler(...)`).
3. Add `[handlers.<name>]` and `[routing]` lines in `config.example.toml`
   (and your local `config.toml` to actually enable it).
4. Unit test under `tests/unit/test_<name>.py` with `FakeClaudeSession`
   + `FakeClock`. If the handler talks to outbox/dispatcher, add one
   integration test under `tests/integration/`.

### Add a new trigger

1. `src/daeyeon_bot/triggers/<name>.py` exposing `MANIFEST: TriggerManifest`
   and an async `emit_one(...)` that writes through
   `infra/outbox.insert_event()` + `infra/outbox.enqueue_handler()` in a
   single transaction (the `events.UNIQUE(source, source_dedup_key)`
   constraint is what makes a re-emit a no-op).
2. Register it in `app/registry.py:instantiate_trigger`.
3. `[triggers.<name>] enabled = true` in `config.example.toml`.
4. Wire it into the supervisor (`app/supervisor.py`) if it's a long-running
   poller; otherwise a one-shot CLI command in `cli/dev.py` is enough.

### Add a SQL column or table

1. New file `src/daeyeon_bot/infra/db/migrations/NNN_<short_description>.sql`.
   `IF NOT EXISTS` and additive only. **Never edit existing migrations.**
2. Run `just migrate` locally; verify the new `meta.schema_version`.
3. Update `docs/PLAN.md` §4.1 (the canonical schema dump) and any test
   helper that seeds rows in the affected table.

### Touch outbox / dispatcher

Re-read `CONTRACTS.md` first. These files implement at-least-once +
recovery semantics; a misuse of `claim_one()` or `settle()` corrupts
delivery guarantees silently. Add an integration test under
`tests/integration/` that exercises the new path against a real `aiosqlite`
DB before merging.

### Change boot order or shutdown phases

Update **all three** in the same commit:
- `app/lifecycle.py` (the code + its docstring)
- `docs/PLAN.md` §2.3 / §2.4
- `tests/integration/test_two_phase_shutdown.py` if invariants change

## When in doubt

- Operations: `docs/RUNBOOK.md` (routine ops + Mac/Linux parity + 5 incident playbooks).
- Design questions: `docs/PLAN.md`.
- Interface guarantees: `CONTRACTS.md`.
- Live state: `daeyeon-bot ops doctor && daeyeon-bot inspect status`.

## Active Technologies
- Python 3.12 (`requires-python = ">=3.12,<3.13"` in `pyproject.toml`). + existing — `claude-agent-sdk`, `pydantic` (v2), `pydantic-settings`, `structlog`, `aiosqlite`, `typer`, `keyring`, `uuid-utils`. No new runtime deps; GitHub access goes through the operator's local `gh` CLI via subprocess. (001-github-pr-review-bot)
- SQLite WAL (existing `state.db`). One additive migration: `002_gh_review_requested_state.sql`. (001-github-pr-review-bot)

## Recent Changes
- 001-github-pr-review-bot: Added Python 3.12 (`requires-python = ">=3.12,<3.13"` in `pyproject.toml`). + existing — `claude-agent-sdk`, `pydantic` (v2), `pydantic-settings`, `structlog`, `aiosqlite`, `typer`, `keyring`, `uuid-utils`. No new runtime deps; GitHub access goes through the operator's local `gh` CLI via subprocess.
