-- daeyeon-bot — schema_version=12.
-- Extend `pr_review_audit.status` CHECK to allow 'skipped_not_authored'.
--
-- `[handlers.pr_review].scope` replaces the old `review_self` boolean. With
-- `scope = "self"` the bot reviews ONLY the operator's own PRs, so a PR
-- authored by someone else is now a skip with its own reason — the mirror of
-- the pre-existing 'skipped_self_authored'. Conflating the two would make the
-- audit unable to answer "why did the bot ignore this PR?".
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt following the
-- same recreation pattern as 004. Data is copied verbatim; no existing row
-- changes meaning.
PRAGMA foreign_keys = OFF;

CREATE TABLE pr_review_audit_new (
    id                       INTEGER PRIMARY KEY,
    event_id                 TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    repo                     TEXT NOT NULL,
    pr_number                INTEGER NOT NULL,
    head_sha                 TEXT NOT NULL,
    request_gen              TEXT NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN
                                 ('posted',
                                  'skipped_self_authored',
                                  'skipped_not_authored',
                                  'skipped_withdrawn',
                                  'skipped_too_large',
                                  'skipped_already_reviewed',
                                  'skipped_disallowed_repo',
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

INSERT INTO pr_review_audit_new SELECT * FROM pr_review_audit;

DROP TABLE pr_review_audit;
ALTER TABLE pr_review_audit_new RENAME TO pr_review_audit;

CREATE INDEX IF NOT EXISTS idx_pra_repo_pr_sha
    ON pr_review_audit(repo, pr_number, head_sha);
CREATE INDEX IF NOT EXISTS idx_pra_event ON pr_review_audit(event_id);

PRAGMA foreign_keys = ON;

UPDATE meta SET value = '12' WHERE key = 'schema_version';
