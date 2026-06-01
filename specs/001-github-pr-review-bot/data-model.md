# Phase 1 Data Model — GitHub PR Review Automation Bot

Two new SQLite tables (one migration), a small set of in-memory dataclasses
in `core/pr_review/`, and one new event type. Everything plugs into the
existing `events` / `outbox` / `runs` machinery; no changes to those tables.

---

## 1. SQLite schema additions

### Migration `002_gh_review_requested_state.sql`

Linear, additive, never edited in place (per `CLAUDE.md` §Add a SQL column).
Bumps `meta.schema_version` to `2`.

```sql
-- daeyeon-bot — schema_version=2.
-- Adds the GitHub PR-review-bot's per-PR polling state and per-review audit log.
PRAGMA foreign_keys = ON;

-- Per-PR state the polling trigger maintains so it can recognize re-requests
-- at the same head SHA. One row per (repo, pr_number).
CREATE TABLE IF NOT EXISTS gh_review_requested_state (
    repo            TEXT NOT NULL,           -- "owner/repo"
    pr_number       INTEGER NOT NULL,
    head_sha        TEXT NOT NULL,           -- last-observed head commit SHA
    request_gen     INTEGER NOT NULL,        -- monotonic; increments on re-request or SHA change
    in_pending_set  INTEGER NOT NULL,        -- 0 / 1; was the PR in the last poll's review-requested:@me result?
    last_observed_at TEXT NOT NULL,          -- ISO8601 UTC
    PRIMARY KEY (repo, pr_number)
);
CREATE INDEX IF NOT EXISTS idx_grrs_pending ON gh_review_requested_state(in_pending_set);

-- Per-review audit row. One row per posted (or skipped) review attempt; on
-- force-supersede the row is updated and the prior review_id pushed onto the
-- superseded_review_ids JSON array.
CREATE TABLE IF NOT EXISTS pr_review_audit (
    id                       INTEGER PRIMARY KEY,
    event_id                 TEXT NOT NULL REFERENCES events(id),
    repo                     TEXT NOT NULL,
    pr_number                INTEGER NOT NULL,
    head_sha                 TEXT NOT NULL,
    request_gen              TEXT NOT NULL,    -- INTEGER for auto, "manual_<ts>" for force re-review
    status                   TEXT NOT NULL CHECK (status IN
                                 ('posted',
                                  'skipped_self_authored',
                                  'skipped_withdrawn',
                                  'skipped_too_large',
                                  'skipped_already_reviewed',
                                  'failed')),
    review_id                INTEGER,          -- GitHub's review id; NULL if skipped/failed
    submitted_at             TEXT,             -- ISO8601 UTC; NULL if skipped/failed
    summary_chars            INTEGER,          -- length of Summary body posted
    inline_comment_count     INTEGER,
    superseded_review_ids    TEXT NOT NULL DEFAULT '[]',  -- JSON array of prior review_ids
    persona_skill            TEXT,             -- which persona variant was active
    persona_mtime_ns         INTEGER,          -- mtime_ns at time of review
    error                    TEXT,             -- error message when status='failed'
    created_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pra_repo_pr_sha
    ON pr_review_audit(repo, pr_number, head_sha);
CREATE INDEX IF NOT EXISTS idx_pra_event ON pr_review_audit(event_id);

UPDATE meta SET value = '2' WHERE key = 'schema_version';
```

**Foreign-key cascade**: existing events-retention prune (`app/prune.py`)
already cascades on `events.id` via the FK on `outbox`. We add the same FK
from `pr_review_audit.event_id`. When events are pruned (90-day default),
audit rows go too — same retention story. No new prune logic required.

### Why two tables, not one

`gh_review_requested_state` is **one row per PR** and is updated in place
every poll. `pr_review_audit` is **append-mostly** (updated only on
force-supersede) and persists per-review history. Mixing them would force
either unbounded growth on the polling side or audit-history loss on the
state side.

---

## 2. New event type

Added to the `Event.type` taxonomy (no schema change — `events.type` is
already free-form):

| `type` | Source | Payload schema (JSON) |
|---|---|---|
| `gh.review_requested` | `gh_review_requested` (auto-trigger) | `{repo: str, pr_number: int, head_sha: str, request_gen: int, requested_at: str}` |
| `pr.review.manual` | `manual` (CLI) | `{repo: str, pr_number: int, head_sha: str, request_gen: str, force: bool}` (request_gen=`"manual_<unix_ts>"`) |

**Routing** (added to `config.example.toml`):
```toml
[routing]
"gh.review_requested" = ["pr_review"]
"pr.review.manual"    = ["pr_review"]
```

Both event types route to the **same** handler. The handler reads
`event.payload['force']` (default `false` for the auto path) to decide whether
to honor the supersede check.

### `events.source_dedup_key` formulas

| Source | Formula |
|---|---|
| `gh_review_requested` | `sha256("gh-review-requested|{repo}#{pr}@{head_sha}#{gen}").hex()` |
| `manual` (force=false) | `sha256("manual-pr-review|{repo}#{pr}@{head_sha}#0").hex()` |
| `manual` (force=true)  | `sha256("manual-pr-review|{repo}#{pr}@{head_sha}#manual_{unix_ts}").hex()` |

The existing `UNIQUE(source, source_dedup_key)` on `events` is the only
race-safe enqueue path. Polling-trigger duplicate observations of the same
`(head_sha, gen)` therefore become no-ops at the SQL layer.

---

## 3. In-memory domain types (`src/daeyeon_bot/core/pr_review/`)

Pure dataclasses, stdlib only. No I/O, no SDK references.

### `core/pr_review/types.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

@dataclass(frozen=True, slots=True)
class PullRequestRef:
    repo: str           # "owner/repo"
    pr_number: int
    head_sha: str
    request_gen: str    # str so manual sentinels and ints share the field

@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    additions: int
    deletions: int
    status: str         # "added" | "modified" | "removed" | "renamed" | …
    patch: str | None   # None for binary or oversized files

@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    """Everything the handler hands to Claude for a review."""
    ref: PullRequestRef
    title: str
    body: str
    author_login: str
    requested_reviewer_logins: tuple[str, ...]
    files: tuple[ChangedFile, ...]   # already capped to size budget

@dataclass(frozen=True, slots=True)
class InlineCommentDraft:
    path: str
    line: int
    side: Literal["RIGHT", "LEFT"] = "RIGHT"
    start_line: int | None = None
    body: str = ""

@dataclass(frozen=True, slots=True)
class ReviewDraft:
    """Validated Claude output, ready to ship to GitHub."""
    summary: str
    comments: tuple[InlineCommentDraft, ...]

@dataclass(frozen=True, slots=True)
class PostedReview:
    review_id: int
    submitted_at: datetime
```

### `core/pr_review/persona.py`

```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True, slots=True)
class Persona:
    skill_dir: Path
    name: str           # = skill_dir.name
    body: str           # markdown after frontmatter strip
    mtime_ns: int

    def is_stale(self, *, current_mtime_ns: int) -> bool:
        return current_mtime_ns != self.mtime_ns
```

State transitions (per FR-006, FR-007):

```
[no persona loaded]
       │  read SKILL.md, strip frontmatter, validate
       ▼
[Persona(body, mtime_ns)]
       │
   ┌───┴────────────────────────────────────────────┐
   │                                                │
 next review:                                  validation fail / file gone
   stat → mtime_ns unchanged                        │
   ⇒ reuse                                          ▼
                                          DeadLetter("persona unavailable: …")
   stat → mtime_ns changed
   ⇒ re-read, validate, re-cache
```

### `core/pr_review/audit.py`

```python
@dataclass(frozen=True, slots=True)
class AuditRow:
    id: int
    event_id: str
    repo: str
    pr_number: int
    head_sha: str
    request_gen: str
    status: str          # one of the CHECK enum values
    review_id: int | None
    submitted_at: datetime | None
    summary_chars: int | None
    inline_comment_count: int | None
    superseded_review_ids: tuple[int, ...]
    persona_skill: str | None
    persona_mtime_ns: int | None
    error: str | None
    created_at: datetime
```

---

## 4. State machine — review request lifecycle

```
                              ┌───────────────────────────────────┐
                              │                                   │
trigger emit (auto OR manual) │                                   │
       │                      │                                   │
       ▼                      │                                   │
   events row + outbox row    │                                   │
       │                      │                                   │
       ▼                      │                                   │
   dispatcher.claim_one()     │                                   │
       │                      │                                   │
       ▼                      │                                   │
  pr_review.handle()          │                                   │
       │                      │                                   │
       ├─ already-reviewed?   │ Ack + audit(skipped_already_reviewed)
       │                      ▼
       ├─ self-authored?      Ack + audit(skipped_self_authored)
       │                      (skipped UNLESS [handlers.pr_review].review_self
       │                       = true; when enabled the own PR proceeds and is
       │                       posted as a COMMENT review — GitHub rejects a
       │                       self-APPROVE, so an APPROVE verdict downgrades to
       │                       COMMENT. The trigger discovers own PRs via an
       │                       author:<operator> search unioned into the poll.)
       │                      ▼
       ├─ withdrawn?          Ack + audit(skipped_withdrawn)
       │                      ▼
       ├─ size > budget?      Ack + audit(skipped_too_large) + post "too large" Summary
       │                      ▼
       ├─ persona invalid?    DeadLetter("persona unavailable: …")
       │                      ▼
       ├─ gh transient err?   Retry(rate_limit_backoff)
       │                      ▼
       ├─ gh auth err?        AuthError → daemon halt (exit 78)
       │                      ▼
       ├─ claude transient?   Retry(default_backoff)
       │                      ▼
       ├─ claude malformed
       │  on second try?      DeadLetter("claude returned malformed review")
       │                      ▼
       └─ post review OK      Ack + audit(posted, review_id, submitted_at)
```

Force-supersede branch (manual `--force` only):
```
already-reviewed? + force=true
   ⇒ post new review with "Updated review for SHA <sha> (supersedes …)" header
   ⇒ audit UPDATE: superseded_review_ids := superseded_review_ids ‖ [old_review_id]
   ⇒ audit UPDATE: review_id := new_review_id, submitted_at := now
```

---

## 5. Trigger state machine — `gh_review_requested_state`

```
poll_now = set of (repo, pr, head_sha) returned by `gh api search/issues`
poll_prev = set built from rows where in_pending_set = 1

For each (repo, pr) ∈ poll_now ∪ poll_prev:

  observed_now      = (repo, pr) ∈ poll_now
  row               = SELECT * FROM gh_review_requested_state WHERE (repo, pr) = ...

  CASE 1: row IS NULL AND observed_now
     ⇒ INSERT row(head_sha=now_sha, request_gen=1, in_pending_set=1)
     ⇒ EMIT event with gen=1

  CASE 2: row.in_pending_set = 0 AND observed_now
     # PR re-entered the set (author clicked "Re-request review")
     ⇒ UPDATE row SET head_sha=now_sha, request_gen=row.request_gen+1, in_pending_set=1
     ⇒ EMIT event with gen=row.request_gen+1

  CASE 3: row.in_pending_set = 1 AND observed_now AND row.head_sha != now_sha
     # New push while still in queue
     ⇒ UPDATE row SET head_sha=now_sha, request_gen=row.request_gen+1
     ⇒ EMIT event with gen=row.request_gen+1

  CASE 4: row.in_pending_set = 1 AND observed_now AND row.head_sha = now_sha
     # Same request instance, redundant observation
     ⇒ UPDATE last_observed_at only
     ⇒ NO EMIT (the events UNIQUE would no-op anyway)

  CASE 5: row.in_pending_set = 1 AND NOT observed_now
     # PR left the queue (request withdrawn, PR closed, or merged)
     ⇒ UPDATE row SET in_pending_set=0
     ⇒ NO EMIT

  CASE 6: row.in_pending_set = 0 AND NOT observed_now
     ⇒ no change; row sits dormant for the next re-entry
```

All UPSERT/INSERT/EMIT for one PR happen inside one `aiosqlite` transaction.
The polling pass iterates PR-by-PR; failure on one PR doesn't block the others.

**Stale-row prune**: `app/prune.py` gains a step (configurable, default 90
days) that deletes `gh_review_requested_state` rows where `in_pending_set=0`
AND `last_observed_at < now - retention.gh_state_dormant_days`.

---

## 6. Validation rules (derived from FRs)

| Rule | Source | Enforcement point |
|---|---|---|
| Persona body ≥ 200 chars after frontmatter strip (default; configurable) | FR-007 | `infra/pr_review_persona.py:load_active_persona` |
| Summary required + ≥ 1 char | FR-009, FR-011 | Pydantic `ReviewOutput.summary: min_length=1` |
| Inline comment max body 8000 chars | API limit | Pydantic `InlineComment.body: max_length=8000` |
| Inline comments ≤ 200 per review | request size sanity | Pydantic `ReviewOutput.comments: max_length=200` |
| Inline anchor must fall in a diff hunk | FR-012 | `handlers/pr_review.py:_filter_anchors` (before posting) |
| Size budget: `lines ≤ 1000` AND `files ≤ 50` (defaults) | FR-013 | `handlers/pr_review.py:_check_size` (before Claude call) |
| `event = "COMMENT"` only | FR-010a | `infra/gh_cli.py:post_review` (constant in code, not parameter) |
| PR title/body/labels/etc. never modified | FR-010b | NOT IMPLEMENTED — there's no code path that ever calls a non-review endpoint; verified by `contracts/github-api-surface.md` enumerating exactly the 4 read endpoints + 1 write endpoint we use |
| No secret-looking content in posted Summary/comments | FR-015, SC-008 | redaction processor in `infra/logging.py` is reused; the Claude system prompt also instructs "do not echo secrets" + Pydantic post-validate runs the same regex set |

---

## 7. Configuration model additions (`config.example.toml`)

```toml
[github]
# Operator's GitHub username. Resolved at boot via `gh api user` if absent.
# Used for self-authored skip and as the search-query subject.
username = ""

# Per-call timeout for `gh api` invocations (seconds).
gh_call_timeout_seconds = 30

[triggers.gh_review_requested]
enabled = true
poll_interval_seconds = 300

[handlers.pr_review]
enabled = true
idempotent = true
dedup_ttl_seconds = 86400
concurrency = 1
accepts = ["gh.review_requested", "pr.review.manual"]

# Active persona (skill directory name under ~/.claude/skills/<name>/SKILL.md).
persona_skill = "pr-review"

# Minimum body length after frontmatter strip; below this, persona is invalid.
min_persona_chars = 200

[handlers.pr_review.size_budget]
max_lines = 1000      # additions + deletions across all changed files
max_files = 50        # changed-file count

[retention]
# how long dormant gh_review_requested_state rows live before prune
gh_state_dormant_days = 90
```

The new pydantic models (`GitHubConfig`, `GhReviewRequestedTriggerEntry`,
`PrReviewHandlerEntry`, `SizeBudget`) live in `app/config.py` and are wired
into `app/registry.py`'s explicit `if name == "pr_review"` branch.

---

## 8. Backward compatibility

- `001_init.sql` is untouched. The new migration is purely additive (two new
  tables, one `meta` UPDATE).
- Existing `manual` trigger and `echo` handler are unchanged.
- Existing routing entries in `config.toml` are unaffected.
- New CLI command `dev fire pr-review` is additive; the existing
  `dev fire manual` continues to work.
- Migration `002_*.sql` runs idempotently (`IF NOT EXISTS`), so re-applying on
  a partially-migrated DB is safe — fits the existing migration runner's
  contract.
