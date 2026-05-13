-- daeyeon-bot — schema_version=5.
-- Adds the Jira regression-failure triage bot's per-issue assignment-state
-- tracking and per-event audit log. Mirrors the gh_review_requested_state
-- pattern shipped in migration 002 — per-issue `in_pending_set` flag +
-- monotonic `assignment_gen` counter for re-entry detection.
--
-- See specs/002-jira-triage-bot/data-model.md §1 for the design rationale
-- and §5 for the trigger state machine that drives these tables.
PRAGMA foreign_keys = ON;

-- Per-issue assignment state maintained by the `jira_assigned` polling
-- trigger. One row per issue ever observed in the watched set defined as
-- `(assignee = currentUser() OR "Team" = "<team_name>") AND project IN
-- (<allowed>) AND summary ~ "regression-test" AND status != Closed`.
--
-- The trigger updates this row + writes events/outbox + emits at most one
-- jira.assigned event per (issue_key, assignment_gen) tuple, all in one
-- aiosqlite transaction.
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
--
-- The status CHECK enumerates every terminal outcome the handler can
-- record. New values require a new migration; do not edit this list.
CREATE TABLE IF NOT EXISTS jira_triage_audit (
    id                       INTEGER PRIMARY KEY,
    event_id                 TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    issue_key                TEXT NOT NULL,                      -- e.g. "SSWCI-16787"
    parent_epic_key          TEXT,                               -- e.g. "SSWCI-16784"; NULL if Epic not resolvable
    hostname                 TEXT,                               -- from title regex; NULL on title miss
    tc_name                  TEXT,                               -- e.g. "TC-0033-Dram_test_with_exception"
    branch                   TEXT,                               -- Epic field
    head_sha                 TEXT,                               -- Epic field (40-hex)
    run_id                   TEXT,                               -- parsed from SSH URL in body
    start_ts                 TEXT,                               -- ISO8601 UTC
    end_ts                   TEXT,
    time_window_fallback     INTEGER NOT NULL DEFAULT 0,         -- 1 when Loki window came from created_at +/- 30 min
    comment_seq              TEXT NOT NULL,                      -- "1" for auto/first manual; "manual_<unix_ts>" for force
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
    comment_id               TEXT,                               -- Jira comment id (str); NULL if not posted
    posted_at                TEXT,                               -- ISO8601 UTC
    summary_chars            INTEGER,                            -- len(summary_md) when posted
    evidence_count           INTEGER,                            -- len(evidence) when posted
    superseded_comment_ids   TEXT NOT NULL DEFAULT '[]',         -- JSON array of prior comment ids
    loki_error               TEXT,                               -- short label when Loki fetch failed
    ssh_error                TEXT,                               -- short label when SSH fetch failed
    persona_skill            TEXT,                               -- which persona variant was active
    persona_mtime_ns         INTEGER,                            -- mtime_ns of SKILL.md at triage time
    missing_fields           TEXT NOT NULL DEFAULT '[]',         -- JSON array populated on 'skipped_missing_metadata'
    error                    TEXT,                               -- error message when status='failed'
    created_at               TEXT NOT NULL                       -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_jta_issue ON jira_triage_audit(issue_key);
CREATE INDEX IF NOT EXISTS idx_jta_event ON jira_triage_audit(event_id);
CREATE INDEX IF NOT EXISTS idx_jta_status ON jira_triage_audit(status);

-- One-time seed flag for jira_assigned trigger's cold-start. When unset
-- (or '0'), the trigger's first poll seeds jira_assigned_state with
-- in_pending_set=1 for every observed issue but does NOT emit events.
-- Prevents day-1 thundering-herd retroactive triage. See spec FR-004a.
INSERT OR IGNORE INTO meta(key, value) VALUES ('jira_assigned_state_seeded', '0');

UPDATE meta SET value = '5' WHERE key = 'schema_version';
