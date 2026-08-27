-- daeyeon-bot — schema_version=11.
-- Adds 'comment_only' to pr_autofix_audit.status.
--
-- `[handlers.pr_autofix].fix_enabled = false` triages and replies but never
-- opens a workspace, so no fix is even attempted. That is distinct from
-- 'dry_run' (a fix WAS made, and deliberately not shipped) and from
-- 'all_rejected' (nothing was worth fixing). Conflating them would make the
-- audit lie about what the bot actually did.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt. Data is
-- copied verbatim; no existing row changes meaning.
PRAGMA foreign_keys = OFF;

CREATE TABLE pr_autofix_audit_new (
    id                INTEGER PRIMARY KEY,
    event_id          TEXT    NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    repo              TEXT    NOT NULL,
    pr_number         INTEGER NOT NULL,
    head_sha          TEXT    NOT NULL,
    round             INTEGER NOT NULL,
    status            TEXT    NOT NULL CHECK (status IN
                          ('in_progress',
                           'pushed',
                           'dry_run',
                           'comment_only',
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

INSERT INTO pr_autofix_audit_new SELECT * FROM pr_autofix_audit;

DROP TABLE pr_autofix_audit;
ALTER TABLE pr_autofix_audit_new RENAME TO pr_autofix_audit;

CREATE INDEX IF NOT EXISTS idx_pafa_event ON pr_autofix_audit(event_id);
CREATE INDEX IF NOT EXISTS idx_pafa_repo_pr ON pr_autofix_audit(repo, pr_number, created_at);

PRAGMA foreign_keys = ON;

UPDATE meta SET value = '11' WHERE key = 'schema_version';
