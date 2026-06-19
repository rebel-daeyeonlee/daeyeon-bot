-- daeyeon-bot — schema_version=6.
-- Adds the CI Monitor / OnCall triage bot's per-channel Slack read cursor and
-- per-event triage audit log (feature 003). The trigger source is a polling
-- read of the two SSW DevOps on-call Slack channels, so the state table is a
-- per-channel high-water cursor rather than the per-entity membership flag the
-- gh_review_requested_state (002) / jira_assigned_state (005) tables use.
--
-- See specs/003-ci-monitor-bot/plan.md §Data Model and §Trigger state machine.
PRAGMA foreign_keys = ON;

-- Per-channel read cursor maintained by the `slack_ci_alert` polling trigger.
-- One row per watched channel. `last_seen_ts` is the high-water Slack message
-- ts; `seeded` is 0 until the cold-start seed completes (first poll anchors the
-- cursor to the channel's current latest ts and emits nothing — no retroactive
-- triage). The trigger advances the cursor + writes events/outbox per emitted
-- candidate in one aiosqlite transaction.
CREATE TABLE IF NOT EXISTS slack_ci_alert_state (
    channel_id    TEXT NOT NULL PRIMARY KEY,   -- e.g. "C09SEN8MH5M"
    last_seen_ts  TEXT NOT NULL,               -- Slack message ts ("1718800000.001200"); high-water cursor
    seeded        INTEGER NOT NULL DEFAULT 0,   -- 0 until cold-start seed completes; 1 thereafter
    updated_at    TEXT NOT NULL                 -- ISO8601 UTC (poll time)
);

-- Per-triage audit row. One row per posted (or skipped / failed) triage. The
-- status CHECK enumerates every terminal outcome the handler can record; new
-- values require a new migration. The (channel_id, message_ts) pair is the
-- primary idempotency key; (repo, run_id) is the secondary cross-alert guard.
--
-- event_id FKs events(id) ON DELETE CASCADE: this ON DELETE action is
-- load-bearing. app/prune.py does `DELETE FROM events ...` and a NOT NULL
-- reference whose ON DELETE defaulted to NO ACTION would abort the prune
-- transaction. CASCADE lets the audit child prune with its events row, no new
-- prune logic. Copied verbatim from 002 pr_review_audit / 005 jira_triage_audit.
CREATE TABLE IF NOT EXISTS ci_triage_audit (
    id                  INTEGER PRIMARY KEY,
    event_id            TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    channel_id          TEXT NOT NULL,
    message_ts          TEXT NOT NULL,                        -- the alert message ts (idempotency key part 1)
    repo                TEXT,                                 -- "rebellions-sw/ssw-bundle"; NULL when no run link
    run_id              TEXT,                                 -- from actions/runs/<id>; NULL when not extractable
    pr_number           INTEGER,                              -- best-effort
    failed_jobs         TEXT NOT NULL DEFAULT '[]',           -- JSON array of failed job names
    status              TEXT NOT NULL CHECK (status IN (
                            'posted',
                            'skipped_no_run_link',
                            'skipped_not_ci_failure',
                            'skipped_already_triaged',
                            'skipped_log_unavailable',
                            'failed'
                        )),
    attribution         TEXT,                                 -- infra_env | product_regression | flaky | unknown (when posted)
    classification      TEXT,                                 -- infra|environment|test_failure|device_failure|build_failure|dependency|timeout|flaky|permission|unknown
    owner_area          TEXT,                                 -- DevOps|SysFw|SysSol|Connectivity|Driver|HW|Unknown
    confidence          TEXT,                                 -- low|medium|high
    wiki_matches        TEXT NOT NULL DEFAULT '[]',           -- JSON array of matched incident/SDOC paths
    posted_channel_id   TEXT,                                 -- dry_run channel OR original channel (thread reply)
    posted_message_ts   TEXT,                                 -- ts of the bot's posted message; NULL if not posted
    summary_chars       INTEGER,                              -- len(slack body) when posted
    persona_skill       TEXT,
    persona_mtime_ns    INTEGER,
    gh_error            TEXT,                                 -- short label when gh --log-failed failed
    wiki_error          TEXT,                                 -- short label when wiki pull/search failed (incl. wiki-less-degraded)
    error               TEXT,                                 -- error message when status='failed'
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cta_msg ON ci_triage_audit(channel_id, message_ts);
CREATE INDEX IF NOT EXISTS idx_cta_run ON ci_triage_audit(repo, run_id);
CREATE INDEX IF NOT EXISTS idx_cta_event ON ci_triage_audit(event_id);
CREATE INDEX IF NOT EXISTS idx_cta_status ON ci_triage_audit(status);

UPDATE meta SET value = '6' WHERE key = 'schema_version';
