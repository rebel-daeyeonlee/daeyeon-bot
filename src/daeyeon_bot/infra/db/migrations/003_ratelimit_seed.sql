-- Seed the `claude_call` bucket consulted by `dispatcher.run()` before
-- every claim. Defaults: 60 tokens, refilled 1.0/s — a soft per-minute
-- cap that absorbs short bursts but flatlines a runaway loop. Knobs in
-- `[ratelimit]` config override these at boot via UPSERT.
--
-- `last_refill = '1970-01-01T00:00:00+00:00'` makes the first `take()`
-- compute a huge "elapsed" since refill, which then clips to capacity.
-- Equivalent to "bucket starts full" without depending on the migration
-- runtime to know what `now()` is.
INSERT OR IGNORE INTO ratelimit_buckets (name, tokens, capacity, refill_per_sec, last_refill)
VALUES ('claude_call', 60.0, 60.0, 1.0, '1970-01-01T00:00:00+00:00');
