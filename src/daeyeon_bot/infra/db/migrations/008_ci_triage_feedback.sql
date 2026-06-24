-- daeyeon-bot — schema_version=8.
-- Feedback loop (feature 003 D): record whether a posted triage was right, from
-- the operator's reaction on the bot's Slack message (✅ correct / ❌ incorrect).
-- Turns the write-only audit log into an accuracy signal so confidence floors and
-- the persona can be tuned with data instead of guesswork. All nullable/additive.
--
-- See specs/003-ci-monitor-bot/plan.md §D feedback loop.
PRAGMA foreign_keys = ON;

ALTER TABLE ci_triage_audit ADD COLUMN feedback TEXT;        -- correct | incorrect | unsure
ALTER TABLE ci_triage_audit ADD COLUMN feedback_emoji TEXT;  -- raw reaction that decided it
ALTER TABLE ci_triage_audit ADD COLUMN feedback_at TEXT;     -- ISO8601 UTC when recorded

-- The collector scans posted rows still missing feedback within a window.
CREATE INDEX IF NOT EXISTS idx_cta_feedback ON ci_triage_audit(feedback, created_at);
