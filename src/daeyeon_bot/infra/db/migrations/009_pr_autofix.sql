-- daeyeon-bot — schema_version=9.
-- Feature 004: PR review-comment auto-fix loop.
--   `gh_pr_feedback` trigger  → `gh_pr_feedback_state`
--   `pr_autofix` handler      → `pr_autofix_comment` (per-comment ledger)
--                             + `pr_autofix_audit`   (per-event run record)
PRAGMA foreign_keys = ON;

-- Per-PR polling state. Mirrors gh_review_requested_state's shape so the
-- §5 case table (observe / withdraw / re-observe) is reusable, plus a
-- `round` counter that caps the fix<->re-review ping-pong.
CREATE TABLE IF NOT EXISTS gh_pr_feedback_state (
    repo              TEXT    NOT NULL,   -- "owner/repo"
    pr_number         INTEGER NOT NULL,
    head_sha          TEXT    NOT NULL,   -- head commit SHA at the last emit
    round             INTEGER NOT NULL,   -- autofix rounds emitted for this PR
    in_pending_set    INTEGER NOT NULL,   -- 0/1: present in the last author:<me> poll?
    last_observed_at  TEXT    NOT NULL,   -- ISO8601 UTC
    PRIMARY KEY (repo, pr_number)
);
CREATE INDEX IF NOT EXISTS idx_gpfs_pending ON gh_pr_feedback_state(in_pending_set);

-- Per-comment decision ledger. THE loop-termination structure: a comment
-- with a row here is "handled" and never re-enters the pending set.
--
-- Deliberately NO foreign key to events(id): the retention prune deletes
-- events past `events_days`, and an ON DELETE CASCADE here would resurrect
-- 90-day-old comments into the pending set and make the bot re-answer them.
-- `event_id` is stored as plain provenance text.
CREATE TABLE IF NOT EXISTS pr_autofix_comment (
    id            INTEGER PRIMARY KEY,
    repo          TEXT    NOT NULL,
    pr_number     INTEGER NOT NULL,
    comment_id    INTEGER NOT NULL,       -- GitHub comment id (review / issue / review body)
    comment_kind  TEXT    NOT NULL CHECK (comment_kind IN
                      ('review_comment', 'issue_comment', 'review_body')),
    author        TEXT    NOT NULL,
    event_id      TEXT,                   -- provenance only; no FK on purpose
    verdict       TEXT    NOT NULL CHECK (verdict IN
                      ('accepted', 'rejected', 'deferred', 'failed')),
    reason        TEXT,
    commit_sha    TEXT,                   -- commit that addressed it (accepted only)
    replied       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    UNIQUE (repo, pr_number, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_pac_pr ON pr_autofix_comment(repo, pr_number);

-- Per-event run record. `in_progress` is written BEFORE the push stage and
-- flipped afterwards; a row still reading `in_progress` on re-entry means we
-- died mid-push, which is an operator-eyes-only situation (the handler
-- DeadLetters instead of pushing twice).
CREATE TABLE IF NOT EXISTS pr_autofix_audit (
    id                INTEGER PRIMARY KEY,
    event_id          TEXT    NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    repo              TEXT    NOT NULL,
    pr_number         INTEGER NOT NULL,
    head_sha          TEXT    NOT NULL,
    round             INTEGER NOT NULL,
    status            TEXT    NOT NULL CHECK (status IN
                          ('in_progress',
                           'pushed',
                           'no_changes',
                           'all_rejected',
                           'verify_failed',
                           'skipped_not_author',
                           'skipped_disallowed_repo',
                           'skipped_max_rounds',
                           'skipped_closed',
                           'skipped_nothing_pending',
                           'skipped_diff_too_large',
                           'skipped_protected_path',
                           'failed')),
    accepted_count    INTEGER,
    rejected_count    INTEGER,
    deferred_count    INTEGER,
    commit_sha        TEXT,
    pushed_at         TEXT,
    changed_files     INTEGER,
    changed_lines     INTEGER,
    verify_command    TEXT,
    verify_exit_code  INTEGER,
    persona_skill     TEXT,
    persona_mtime_ns  INTEGER,
    error             TEXT,
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pafa_event ON pr_autofix_audit(event_id);
CREATE INDEX IF NOT EXISTS idx_pafa_repo_pr ON pr_autofix_audit(repo, pr_number, created_at);

UPDATE meta SET value = '9' WHERE key = 'schema_version';
