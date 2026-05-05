-- daeyeon-bot — schema_version=4.
-- Extend `pr_review_audit.status` CHECK constraint to allow
-- 'skipped_disallowed_repo' for the repo-allowlist gate (handler-side
-- defense-in-depth). SQLite cannot ALTER a CHECK constraint in place, so
-- follow the official 12-step recreation pattern
-- (https://sqlite.org/lang_altertable.html#otheralter): rename old table
-- aside, create the new table with the extended constraint, copy rows,
-- drop the old table, rename, and re-create indexes.
PRAGMA foreign_keys = ON;

ALTER TABLE pr_review_audit RENAME TO pr_review_audit__old;

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

INSERT INTO pr_review_audit
SELECT * FROM pr_review_audit__old;

DROP TABLE pr_review_audit__old;

CREATE INDEX IF NOT EXISTS idx_pra_repo_pr_sha
    ON pr_review_audit(repo, pr_number, head_sha);
CREATE INDEX IF NOT EXISTS idx_pra_event ON pr_review_audit(event_id);

UPDATE meta SET value = '4' WHERE key = 'schema_version';
