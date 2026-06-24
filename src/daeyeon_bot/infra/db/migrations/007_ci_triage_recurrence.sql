-- daeyeon-bot — schema_version=7.
-- Recurrence-detection columns on ci_triage_audit (feature 003 P2). Turns the
-- write-only audit log into a recurrence detector: `signature` is a
-- host-agnostic normalized failure key ("같은 종류 실패가 7일 N회"), `dut_host`
-- is the resolved device-under-test host ("이 호스트 또 그러네"). Both are
-- nullable and additive — old rows simply carry NULL and never match a count.
--
-- See specs/003-ci-monitor-bot/plan.md §P2 recurrence.
PRAGMA foreign_keys = ON;

ALTER TABLE ci_triage_audit ADD COLUMN dut_host TEXT;
ALTER TABLE ci_triage_audit ADD COLUMN signature TEXT;

-- Recurrence queries scan by (signature, created_at) and (dut_host, created_at).
CREATE INDEX IF NOT EXISTS idx_cta_signature ON ci_triage_audit(signature, created_at);
CREATE INDEX IF NOT EXISTS idx_cta_dut_host ON ci_triage_audit(dut_host, created_at);
