# Phase 1 Data Model — Jira Regression-Failure Triage Bot

Two new SQLite tables (one migration), a set of in-memory dataclasses in
`core/jira_triage/`, and one new event type. Everything plugs into the
existing `events` / `outbox` / `runs` machinery; no changes to those tables.

---

## 1. SQLite schema additions

### Migration `005_jira_triage_state.sql`

Linear, additive, never edited in place (per `CLAUDE.md` §Add a SQL column).
Bumps `meta.schema_version` to `5`.

```sql
-- daeyeon-bot — schema_version=5.
-- Adds the Jira triage bot's per-issue assignment-state tracking and per-event audit log.
-- Mirrors the gh_review_requested_state pattern (see 002_*.sql) — per-issue `in_pending_set`
-- flag + monotonic `assignment_gen` counter for re-entry detection.
PRAGMA foreign_keys = ON;

-- Per-issue assignment state. The trigger uses this to detect:
-- (a) a ticket entering the set for the first time (insert with assignment_gen=1, emit),
-- (b) a ticket re-entering the set after leaving (in_pending_set flips 0→1, gen += 1, emit),
-- (c) a ticket leaving the set (in_pending_set flips 1→0, no emit).
-- "Entering the set" = matching the watched JQL (assignee=me OR Team=DevOps + project + title filter).
CREATE TABLE IF NOT EXISTS jira_assigned_state (
    issue_key        TEXT NOT NULL PRIMARY KEY,         -- e.g. "SSWCI-16787"
    project          TEXT NOT NULL,                     -- e.g. "SSWCI"
    in_pending_set   INTEGER NOT NULL,                  -- 0 / 1; was the issue in the last poll's watched set?
    assignment_gen   INTEGER NOT NULL,                  -- monotonic; increments on every re-entry into the set
    last_observed_at TEXT NOT NULL                      -- ISO8601 UTC (poll mtime)
);
CREATE INDEX IF NOT EXISTS idx_jas_pending ON jira_assigned_state(in_pending_set);
CREATE INDEX IF NOT EXISTS idx_jas_project ON jira_assigned_state(project);

-- Per-triage audit row. One row per posted (or skipped / failed) triage
-- attempt; on force-supersede the row is updated and the prior comment_id
-- is appended to the superseded_comment_ids JSON array.
CREATE TABLE IF NOT EXISTS jira_triage_audit (
    id                       INTEGER PRIMARY KEY,
    event_id                 TEXT NOT NULL REFERENCES events(id),
    issue_key                TEXT NOT NULL,                      -- e.g. "SSWCI-16787"
    parent_epic_key          TEXT,                               -- e.g. "SSWCI-16784"; NULL if Epic not resolvable
    hostname                 TEXT,                               -- parsed from title; NULL only when title regex missed
    tc_name                  TEXT,                               -- e.g. "TC-0033-Dram_test_with_exception"; NULL when title miss
    branch                   TEXT,                               -- from Epic field; NULL when missing
    head_sha                 TEXT,                               -- 40-hex; NULL when commit field missing
    run_id                   TEXT,                               -- from SSH URL; NULL when not in body
    start_ts                 TEXT,                               -- ISO8601 UTC; NULL when body parse failed
    end_ts                   TEXT,                               -- ISO8601 UTC; NULL when body parse failed
    time_window_fallback     INTEGER NOT NULL DEFAULT 0,         -- 1 when Loki window came from created_at ± 30 min
    comment_seq              TEXT NOT NULL,                      -- "1" for auto/first manual, "manual_<unix_ts>" for force
    status                   TEXT NOT NULL CHECK (status IN (
                                  'posted',
                                  'skipped_not_regression_failure',
                                  'skipped_missing_metadata',
                                  'skipped_unresolvable_commit',
                                  'skipped_submodule_failure',
                                  'skipped_already_triaged',
                                  'failed'
                              )),
    domain                   TEXT,                               -- TriageOutput.domain when status='posted'
    severity                 TEXT,                               -- TriageOutput.severity when status='posted'
    comment_id               TEXT,                               -- Jira comment id (str — Jira returns string); NULL if not posted
    posted_at                TEXT,                               -- ISO8601 UTC; NULL when not posted
    summary_chars            INTEGER,                            -- len(summary_md) when posted
    evidence_count           INTEGER,                            -- len(evidence) when posted
    superseded_comment_ids   TEXT NOT NULL DEFAULT '[]',         -- JSON array of prior comment_ids
    loki_error               TEXT,                               -- short label when Loki fetch failed
    ssh_error                TEXT,                               -- short label when SSH fetch failed
    persona_skill            TEXT,                               -- which persona variant was active
    persona_mtime_ns         INTEGER,                            -- mtime_ns at time of triage
    missing_fields           TEXT NOT NULL DEFAULT '[]',         -- JSON array — populated when status='skipped_missing_metadata'
    error                    TEXT,                               -- error message when status='failed'
    created_at               TEXT NOT NULL                       -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_jta_issue ON jira_triage_audit(issue_key);
CREATE INDEX IF NOT EXISTS idx_jta_event ON jira_triage_audit(event_id);
CREATE INDEX IF NOT EXISTS idx_jta_status ON jira_triage_audit(status);

UPDATE meta SET value = '5' WHERE key = 'schema_version';
```

**Foreign-key cascade**: existing events-retention prune (`app/prune.py`)
cascades on `events.id` via the FK on `outbox`. We add the same FK from
`jira_triage_audit.event_id`. When events are pruned (90-day default),
audit rows go too — same retention story. No new prune logic required.

### Why two tables, not one

`jira_assigned_state` is **one row per issue** and is updated in place
every poll (`in_pending_set` flips, `last_observed_at` refresh, occasional
`assignment_gen` increment). `jira_triage_audit` is **append-mostly**
(updated only on force-supersede) and persists per-triage history. Mixing
them would force either unbounded state-row count growth tied to history
or audit-history loss on the state side.

### Stale-row prune

`app/prune.py` gains a step (configurable, default 180 days) that deletes
`jira_assigned_state` rows where `in_pending_set=0 AND last_observed_at <
now - retention.jira_state_dormant_days`. This mirrors the
`gh_review_requested_state` prune logic shipped in 002.

---

## 2. New event type

Added to the `Event.type` taxonomy (no schema change — `events.type` is
already free-form):

| `type` | Source | Payload schema (JSON) |
|---|---|---|
| `jira.assigned` | `jira_assigned` (auto-trigger) | `{issue_key: str, project: str, assignment_gen: int, assignee_path: "user"\|"team", observed_at: str}` |
| `jira.triage.manual` | `manual` (CLI) | `{issue_key: str, force: bool, comment_seq: str}` (`comment_seq="manual_<unix_ts>"` when force=true, `"1"` otherwise) |

**Routing** (added to `config.example.toml`):
```toml
[routing]
"jira.assigned"       = ["jira_triage"]
"jira.triage.manual"  = ["jira_triage"]
```

Both event types route to the **same** handler. The handler reads
`event.payload['force']` (default `false` for the auto path; the auto event
doesn't carry a `force` key) to decide whether to honor the supersede check.

`assignee_path` captures whether the ticket entered the watched set via
direct assignee match (`"user"`) or via team match (`"team"`). The
handler logs it for audit but does not branch on it — both paths run the
same pipeline.

### `events.source_dedup_key` formulas

| Source | Formula |
|---|---|
| `jira_assigned` | `sha256("jira-assigned|{key}|{assignment_gen}").hex()` |
| `manual` (force=false) | `sha256("manual-jira-triage|{key}|1").hex()` |
| `manual` (force=true)  | `sha256("manual-jira-triage|{key}|manual_{unix_ts}").hex()` |

The existing `UNIQUE(source, source_dedup_key)` on `events` is the only
race-safe enqueue path. Polling-trigger duplicate observations of the same
`(project, key, created_iso)` therefore become no-ops at the SQL layer.

---

## 3. In-memory domain types (`src/daeyeon_bot/core/jira_triage/`)

Pure dataclasses, stdlib only. No I/O, no SDK references, no `httpx`/`asyncssh`.

### `core/jira_triage/types.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

@dataclass(frozen=True, slots=True)
class TicketRef:
    project: str            # "SSWCI"
    issue_key: str          # "SSWCI-16787"
    created_iso: str        # ISO8601 UTC

@dataclass(frozen=True, slots=True)
class TitleParse:
    """Result of regex-parsing the ticket title."""
    hostname: str           # e.g. "ssw-giga-02"
    tc_name: str            # e.g. "TC-0033-Dram_test_with_exception"

@dataclass(frozen=True, slots=True)
class EpicMeta:
    epic_key: str           # e.g. "SSWCI-16784"
    branch: str             # e.g. "release/v3.2"
    commit: str             # 40-hex

@dataclass(frozen=True, slots=True)
class TimeWindow:
    start_ts: datetime
    end_ts: datetime
    fallback: bool          # True when both came from created_at ± 30 min

@dataclass(frozen=True, slots=True)
class SshDumpLocation:
    host: str               # extracted from ssh:// URL (== hostname from title in well-formed tickets)
    remote_path: str        # "/mnt/data/logs/regression-test/<run-id>/<host>/<TC>"
    run_id: str             # e.g. "25746526668-1"

@dataclass(frozen=True, slots=True)
class RunMeta:
    """Everything the handler resolved before collecting data."""
    ticket: TicketRef
    title: TitleParse
    epic: EpicMeta
    window: TimeWindow
    ssh: SshDumpLocation | None
    host_ip: str | None       # DNS-resolved; None when DNS failed (fwlog/smclog then skipped)

LokiStream = Literal["fwlog", "smclog", "kernel", "syslog"]

@dataclass(frozen=True, slots=True)
class LokiSlice:
    stream: LokiStream
    lines: tuple[str, ...]
    truncated: bool

@dataclass(frozen=True, slots=True)
class SshArtifact:
    filename: str             # e.g. "output.xml"
    size_bytes: int
    contents: str | None      # None if oversized (skipped)

@dataclass(frozen=True, slots=True)
class ProductCodeFile:
    submodule_path: str       # e.g. "products/common/kmd"
    file_path: str            # repo-relative
    excerpt: str              # capped per source-budget knob

@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Everything the handler hands to Claude for a triage."""
    meta: RunMeta
    error_log_excerpt: str        # extracted from ticket body
    test_code: str | None         # contents of <tc>.robot; None if not found in suites tree
    product_code: tuple[ProductCodeFile, ...]
    loki_slices: tuple[LokiSlice, ...]
    ssh_artifacts: tuple[SshArtifact, ...]
    loki_error: str | None
    ssh_error: str | None

Domain = Literal["Driver", "SysFw", "CpFw", "SysSol", "DevOps", "Connectivity", "unknown"]
Severity = Literal["sev1", "sev2", "sev3", "unknown"]

@dataclass(frozen=True, slots=True)
class EvidenceItem:
    source: str         # "loki.fwlog" | "loki.smclog" | "loki.kernel" | "loki.syslog" | "ssh.output_xml" | "ssh.dmesg" | "ssh.console" | "test_code" | "product_code"
    quote: str
    citation: str       # "file:line" or ISO8601 timestamp

@dataclass(frozen=True, slots=True)
class SuspectedDuplicate:
    key: str            # e.g. "SSWCI-16012"
    basis: str

@dataclass(frozen=True, slots=True)
class TriageDraft:
    """Validated Claude output, ready to ship to Jira."""
    summary_md: str
    domain: Domain
    severity: Severity
    suspected_duplicates: tuple[SuspectedDuplicate, ...]
    needs_human: bool
    evidence: tuple[EvidenceItem, ...]

@dataclass(frozen=True, slots=True)
class PostedComment:
    comment_id: str
    posted_at: datetime
```

### `core/jira_triage/audit.py`

```python
@dataclass(frozen=True, slots=True)
class AuditRow:
    id: int
    event_id: str
    issue_key: str
    parent_epic_key: str | None
    hostname: str | None
    tc_name: str | None
    branch: str | None
    head_sha: str | None
    run_id: str | None
    start_ts: datetime | None
    end_ts: datetime | None
    time_window_fallback: bool
    comment_seq: str
    status: str          # one of the CHECK enum values
    domain: str | None
    severity: str | None
    comment_id: str | None
    posted_at: datetime | None
    summary_chars: int | None
    evidence_count: int | None
    superseded_comment_ids: tuple[str, ...]
    loki_error: str | None
    ssh_error: str | None
    persona_skill: str | None
    persona_mtime_ns: int | None
    missing_fields: tuple[str, ...]
    error: str | None
    created_at: datetime
```

### `core/jira_triage/persona.py`

The Persona dataclass is **reused from `core/persona.py`** (refactored
out of `core/pr_review/persona.py` per `research.md` R6). No duplicate
definition here.

---

## 4. State machine — triage event lifecycle

```
                          ┌────────────────────────────────────────────────────┐
                          │                                                    │
trigger emit (auto OR     │                                                    │
manual)                   │                                                    │
       │                  │                                                    │
       ▼                  │                                                    │
   events row +           │                                                    │
   outbox row             │                                                    │
       │                  │                                                    │
       ▼                  │                                                    │
   dispatcher.claim_one() │                                                    │
       │                  │                                                    │
       ▼                  │                                                    │
   jira_triage.handle()   │                                                    │
       │                  │                                                    │
       ├─ title regex     │  Ack + audit(skipped_not_regression_failure)
       │  miss?           │
       │                  ▼
       ├─ persona invalid?    DeadLetter("persona unavailable: …")
       │                      ▼
       ├─ Epic field missing? Ack + audit(skipped_missing_metadata, missing_fields=[…])
       │                      ▼
       ├─ already-triaged
       │  (force=False)?      Ack + audit(skipped_already_triaged)
       │                      ▼
       ├─ ssw-bundle checkout
       │  unresolvable?       Ack + audit(skipped_unresolvable_commit)
       │                      ▼
       ├─ submodule init
       │  failure?            Ack + audit(skipped_submodule_failure)
       │                      ▼
       ├─ (continue with partial: Loki / SSH failures populate audit.loki_error /
       │   ssh_error but do NOT skip)
       │                      ▼
       ├─ claude transient?   Retry(default_backoff)
       │                      ▼
       ├─ claude malformed
       │  on second try?      DeadLetter("claude returned malformed triage")
       │                      ▼
       ├─ redaction match?    DeadLetter("redaction would alter posted content")
       │                      ▼
       ├─ jira 401?           AuthError → daemon halt (exit 78)
       │                      ▼
       ├─ jira 429?           Retry(rate_limit_backoff)
       │                      ▼
       ├─ jira 5xx?           Retry(default_backoff)
       │                      ▼
       ├─ asyncio.wait_for
       │  exceeded budget,
       │  first time?         Retry(default_backoff)
       │                      ▼
       ├─ asyncio.wait_for
       │  exceeded budget,
       │  second time?        DeadLetter("triage timed out twice")
       │                      ▼
       └─ post comment OK     Ack + audit(posted, comment_id, posted_at, domain, severity)
```

Force-supersede branch (manual `--force` only):
```
already-triaged? + force=true
   ⇒ post new comment with "Updated triage (supersedes …)" header
   ⇒ audit UPDATE: superseded_comment_ids := superseded_comment_ids ‖ [old_comment_id]
   ⇒ audit UPDATE: comment_id := new_comment_id, posted_at := now
```

---

## 5. Trigger state machine — `jira_assigned`

Mirrors `gh_review_requested` (feature 001's polling trigger). The
trigger maintains per-issue state and emits events on transitions, not
on observations.

```
# One JQL query per poll cycle for the union of all allowed projects:
JQL = (assignee = currentUser() OR "Team" = "<team_name>")
      AND project IN ("<P1>", "<P2>", ...)
      AND summary ~ "regression-test"
      AND status != Closed

page_now  = set of (issue_key, project, which_match) from JQL page
          # which_match ∈ {"user","team"} based on which clause matched;
          # determined by re-checking each ticket's assignee + team
          # fields in the response, since JQL itself doesn't tell us.

page_prev = set built from rows where in_pending_set = 1

For each issue ∈ page_now ∪ page_prev:
  observed_now = issue ∈ page_now
  row          = SELECT * FROM jira_assigned_state WHERE issue_key = ...

  CASE 1: row IS NULL AND observed_now
     # First-ever observation
     ⇒ INSERT row(in_pending_set=1, assignment_gen=1)
     ⇒ EMIT event with gen=1, assignee_path=which_match

  CASE 2: row.in_pending_set = 0 AND observed_now
     # Re-entered the set (was unassigned, reassigned now)
     ⇒ UPDATE row SET in_pending_set=1, assignment_gen=row.assignment_gen+1
     ⇒ EMIT event with gen=row.assignment_gen+1, assignee_path=which_match

  CASE 3: row.in_pending_set = 1 AND observed_now
     # Redundant observation (still assigned). NO emit; UNIQUE would no-op anyway.
     ⇒ UPDATE last_observed_at only
     ⇒ NO EMIT

  CASE 4: row.in_pending_set = 1 AND NOT observed_now
     # Left the set (unassigned, reassigned to someone else, or closed)
     ⇒ UPDATE row SET in_pending_set=0
     ⇒ NO EMIT

  CASE 5: row.in_pending_set = 0 AND NOT observed_now
     # Dormant; no change.
```

All UPSERT/INSERT/EMIT for one issue happen inside one `aiosqlite`
transaction. The polling pass iterates issue-by-issue; failure on one
issue doesn't block the others.

**Pagination**: If a page is full (`maxResults`), the trigger fetches
the next page in the same cycle, up to a safety cap of `200` issues per
cycle (`[triggers.jira_assigned].max_per_cycle`).

**Cold-start**: On the very first poll after the daemon's birth (state
table empty), the trigger does NOT emit events for the issues it
observes. It just seeds `(issue_key, in_pending_set=1, assignment_gen=1,
last_observed_at=now)` rows for everything in `page_now` and exits the
cycle. This prevents thundering-herd behavior on day-1 deploy — the bot
won't retroactively triage 30 tickets that have been sitting in
daeyeon's queue for weeks.

A boolean flag `jira_assigned_state_seeded` is stored as a single row in
`meta` table (set to `'1'` after the cold-start seed completes) so a
subsequent restart doesn't accidentally re-seed-and-skip.

**Why `assignment_gen` per issue, not a global counter**: matches the
`gh_review_requested.request_gen` semantic. A single issue can be
re-assigned to me multiple times across its lifetime; each re-entry is
a distinct triage request instance. Global counters would conflate
distinct issues' assignment histories.

---

## 6. Validation rules (derived from FRs)

| Rule | Source | Enforcement point |
|---|---|---|
| Persona body ≥ 200 chars after frontmatter strip (default; configurable) | (shared with 001) | `infra/persona_loader.py` |
| `summary_md` required + ≥1 char | FR-016, FR-019 | Pydantic `TriageOutput.summary_md: min_length=1` |
| `evidence` required when `domain != "unknown"` | FR-017 | Pydantic `@model_validator` on `TriageOutput` |
| `evidence_item.body` ≤ 2000 chars (quote) + ≤ 512 chars (citation) | output sanity | Pydantic field constraints |
| `suspected_duplicates` ≤ 5 items, each key matches `^[A-Z]+-\d+$` | output sanity | Pydantic |
| Title regex MUST match for auto events; for manual events, title-miss is `skipped_not_regression_failure` | FR-002, FR-004 | `handlers/jira_triage.py:_parse_title` |
| Epic MUST have non-empty `branch` + `commit` (40-hex) | FR-005 | `handlers/jira_triage.py:_resolve_epic` |
| ssw-bundle clone path MUST be inside `project_root` (unless `allow_external_ssw_bundle=true`) | FR-009 | `infra/ssw_bundle.py:__init__` |
| ssw-bundle ops MUST NEVER `push`/`commit`/`reset --hard` outside detach | FR-012 | `infra/ssw_bundle.py` exposes only `ensure_checkout()` + `read_file()`; no push/commit method exists |
| Comment body MUST be ADF | FR-018 | `infra/jira_client.py:post_comment` accepts only ADF (`dict`), never a raw string |
| Only one Jira write endpoint MUST be used | FR-018 | `contracts/jira-rest-api-surface.md` enumerates; banned list explicit |
| Per-event wall-clock ≤ 600 s (default) | FR-031 | `handlers/jira_triage.py` wraps `handle()` in `asyncio.wait_for(...)` |
| No secret-looking content in posted comment | FR-022, SC-008 | redaction processor reused; strict mode (match → DeadLetter) on comment body, mirroring pr_review |

---

## 7. Configuration model additions (`config.example.toml`)

```toml
[jira]
# Atlassian Cloud host. Single-tenant.
base_url = "https://rbln.atlassian.net"
# Optional override for the discovered "TC Failure" issuetype name.
# Leave empty for autodiscovery via getJiraIssueTypeMetaWithFields at boot.
issuetype_override = ""
# Per-call timeout for httpx (seconds).
timeout_seconds = 30

[loki]
base_url = "http://loki.ssw.rbln.in"
# Per-stream byte cap; longer slices get truncated client-side.
per_stream_max_bytes = 1048576
# Per-request timeout for httpx (seconds).
timeout_seconds = 30
# Loki kernel/syslog label schema override.
# Leave default unless promtail/vector config diverges.
kernel_query_template = '{hostname="{host}", job=~"varlogs|systemd-journal", filename=~".*kern.*"}'
syslog_query_template = '{hostname="{host}", job=~"varlogs|systemd-journal", filename=~".*syslog.*"}'

[triggers.jira_assigned]
enabled = false                      # default off; flip in local config to start polling
poll_interval_seconds = 300          # 5 min
max_per_cycle = 200                  # safety cap on issues fetched per poll cycle
team_name = "DevOps"                 # also match tickets assigned to this team; empty string disables team match

[handlers.jira_triage]
enabled = false
idempotent = true
dedup_ttl_seconds = 86400
concurrency = 1
accepts = ["jira.assigned", "jira.triage.manual"]
allowed_projects = ["SSWCI"]
persona_skill = "daeyeon-bot-jira-triage"
min_persona_chars = 200
timeout_seconds = 600                # per-event wall-clock
ssw_bundle_path = "var/ssw-bundle"   # project-root-relative; absolute path also accepted
allow_external_ssw_bundle = false    # safety guard
ssh_known_hosts_path = "jira_triage_known_hosts"   # under state_dir
ssh_max_file_bytes = 10485760        # 10 MB
ssh_fetch_globs = ["output.xml", "dmesg.log", "console.log"]
# Custom-field IDs override (leave empty for autodiscovery)
branch_field_id = ""
commit_field_id = ""
team_field_id = ""                   # Jira Atlassian Teams field; leave empty for autodiscovery

[retention]
# Existing knobs unchanged. New knob for the assignee-state prune:
jira_state_dormant_days = 180        # prune dormant jira_assigned_state rows after N days

[retention]
# Existing knobs unchanged. Audit rows prune with their referenced event_id
# via the standard events retention.
```

The new pydantic models (`JiraConfig`, `LokiConfig`,
`JiraAssignedTriggerEntry`, `JiraTriageHandlerEntry`) live in
`app/config.py` and are wired into `app/registry.py`'s explicit
`if name == "jira_assigned"` / `if name == "jira_triage"` branches.

---

## 8. Backward compatibility

- All earlier migrations are untouched. The new migration is purely
  additive (two new tables, one `meta` UPDATE).
- Existing `manual` / `gh_review_requested` triggers and `echo` / `pr_review`
  handlers are unchanged.
- Existing routing entries in `config.toml` are unaffected.
- New CLI commands `dev fire jira-triage` and `inspect jira-triage` are
  additive.
- Migration `005_*.sql` runs idempotently (`IF NOT EXISTS`), so re-applying
  on a partially-migrated DB is safe.
- The `core/persona.py` refactor (R6) re-exports `Persona` from
  `core/pr_review/__init__.py` for one release, so callers of
  `from daeyeon_bot.core.pr_review.persona import Persona` continue to
  work.
