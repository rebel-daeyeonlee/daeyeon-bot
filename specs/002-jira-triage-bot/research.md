# Phase 0 Research — Jira Regression-Failure Triage Bot

Resolves every open question raised by the spec's Technical Context and the
project's existing contracts. Each entry follows: **Decision → Rationale →
Alternatives**.

---

## R1. Jira authentication

**Decision**: Direct REST API access via `httpx.AsyncClient` against
`https://rbln.atlassian.net/`. **Basic auth** `(JIRA_USER,
JIRA_API_TOKEN)` — both keys live in the daemon's secrets provider
chain. Names match `ssw-bundle/inv/test_report/jira_client.py:11-24`,
which establishes the convention the operator already uses. Bot calls
`GET /rest/api/3/myself` at boot to confirm the credentials are valid
and `JIRA_USER` matches the returned `emailAddress` (raises `AuthError`
⇒ daemon halts, exit 78, if not). No MCP server on the hot path.

**Rationale**:
- Spec FR-020 nailed this. The operator already generates API tokens for
  other tooling; reusing the same `JIRA_USER` + `JIRA_API_TOKEN` workflow
  eliminates a new login UX AND keeps the daeyeon-bot daemon's auth
  shape consistent with `ssw-bundle/inv/test_report/jira_client.py`
  (which uses `JIRA(options={"server": server}, basic_auth=(user,
  token))`). One token rotation step covers both tools.
- Atlassian MCP is great for interactive work but introduces a network hop
  + a stateful MCP session, both of which compound failure modes for an
  unattended daemon. The bot's hot path needs deterministic latency and
  clear error mapping; MCP would muddy both.
- `httpx` is the smallest reasonable HTTP client and already commonly
  pinned alongside `pydantic` in the Python async ecosystem.

**Alternatives**:
- Atlassian MCP server (rejected — added latency, opaque error surface,
  not designed for single-purpose daemon hot paths).
- `atlassian-python-api` library (rejected — sync-only, drags `requests`).
- `aiohttp` (rejected — `httpx` is more ergonomic and already commonly used
  alongside pydantic).

**Implementation note**: All Jira REST calls go through one async wrapper
at `infra/jira_client.py` so error mapping, structured-log redaction, and
retry-budget live in one place. Tests inject `httpx.MockTransport` or a
`FakeJira` substitute via the container.

---

## R2. Polling vs. webhook for assignment-triggered triage

**Decision**: Polling. One trigger task wakes every `poll_interval_seconds`
(default 300) and runs:

```
GET /rest/api/3/search?jql=(assignee = currentUser() OR "Team" = "{team_name}")
                            AND project IN ({allowed})
                            AND summary ~ "regression-test"
                            AND status != Closed
                       &fields=key,summary,assignee,parent,status,<team_field_id>
                       &maxResults=50
```

The result is a **set** of issue keys currently in daeyeon's (or
DevOps's) watched queue. The trigger reconciles this against
`jira_assigned_state` to detect transitions (mirrors the
`gh_review_requested` flow):
- issue enters the set for the first time ⇒ `assignment_gen=1` event
- issue leaves and re-enters the set ⇒ `assignment_gen += 1` event
- issue remains in the set ⇒ no event (UNIQUE would no-op anyway)
- issue leaves the set ⇒ flip `in_pending_set=0`, no event

**Rationale**:
- Atlassian Webhooks need a public ingress (ngrok / Cloudflare Tunnel /
  reverse proxy on a static IP). The daemon is single-tenant on the
  operator's laptop/server; standing up ingress is more ops surface than
  the latency saving (5-min p95 vs. seconds) is worth.
- Spec SC-002 allows up to 15 min auto-detection, well within 5-min poll
  cadence + 10-min handler budget.
- The `created >=` cursor is monotonically advancing; a crash between
  fetch and enqueue is safe because re-poll with the un-advanced cursor
  hits `events.UNIQUE(source, source_dedup_key)` and no-ops.

**Rationale for assignment-trigger vs. `created >=` time-window trigger**:
- daeyeon explicitly chose this (clarification 2026-05-13): the bot
  should only triage tickets that actually entered his queue, not every
  new ticket that exists. Assignment is the signal of "needs attention".
- The team-level expansion (`OR "Team" = "DevOps"`) covers tickets that
  are assigned to the team rather than to a specific human — those are
  exactly the tickets daeyeon would manually pick up on rotation.
- The state-table model (`in_pending_set` + `assignment_gen`) is a
  direct mirror of `gh_review_requested_state`. Reusing the same shape
  means operators learn one pattern, not two.

**Alternatives**:
- Jira Webhooks (rejected — ingress + secret + replay handling, all for
  sub-minute latency the spec doesn't need).
- Long-poll (no such endpoint exists for issue creation in Jira REST v3).

**Edge handling**:
- Jira returns 429 (rate limit) with `Retry-After` header. Trigger backs
  off using that value and parks itself; existing supervisor's quarantine
  kicks in after 5 fails / 10 min.
- The polling trigger NEVER bypasses outbox; every event goes through
  `infra/outbox.py:insert_event` so dedup, recovery, and replay all behave
  identically to manual-fired events.
- JQL pagination: `maxResults=50` is the default; if a poll cycle returns
  exactly 50 results, the trigger fetches the next page in the same cycle
  until fewer than 50 come back (or a configurable safety cap, default 200).

---

## R3. State table & dedup design

**Decision**: Per-issue state (`jira_assigned_state(issue_key, project,
in_pending_set, assignment_gen, last_observed_at)`) — one row per issue
ever observed in the watched set. `events.source_dedup_key` is the SHA256
of `"jira-assigned|{key}|{assignment_gen}"`. The state row and the
events INSERT + outbox INSERT all happen in one SQLite transaction.

**Rationale**:
- Direct mirror of `gh_review_requested_state` from feature 001 — same
  primitive, same operator mental model.
- `assignment_gen` increments only on re-entry, so the dedup token is
  unique per request instance. Same-instance redundant observations
  (overlapping polls, restart-replay) are no-op'd at the SQL layer.
- Per-issue rows mean the table grows linearly with distinct issues
  ever assigned — bounded by the SSWCI project's lifetime backlog. Prune
  step (R3a below) keeps dormant rows from accumulating indefinitely.

**Concretely** (per issue in the JQL page; runs inside one tx per issue):
```sql
BEGIN;
  -- read current state
  SELECT in_pending_set, assignment_gen FROM jira_assigned_state WHERE issue_key=?;

  -- one of CASES 1-5 from data-model.md §5 fires:
  --   CASE 1: new row + INSERT events + INSERT outbox
  --   CASE 2: UPDATE row (gen += 1, in_pending_set=1) + INSERT events + INSERT outbox
  --   CASE 3: UPDATE last_observed_at only, no emit
  --   CASE 4: UPDATE row (in_pending_set=0), no emit
  --   CASE 5: nothing
COMMIT;
```

**Cold-start guard** (per FR-004a): on first poll after daemon birth
(`meta.jira_assigned_state_seeded != '1'`), all observed issues are
seeded with `in_pending_set=1, assignment_gen=1` but NO events emitted.
After the seed pass, `meta` flag is flipped. Prevents day-1
thundering-herd.

**Alternatives**:
- Watch the Jira "issue assigned" webhook (rejected — webhooks need
  public ingress; same ops issue as feature 001's GitHub-webhook
  alternative).
- Long-poll Jira's notification stream (no such endpoint in REST v3).
- Track only `assignee = currentUser()` (rejected — user explicitly
  added DevOps Team in clarification; team tickets are common in NPU
  regression rotation).

---

## R4. Parent Epic field discovery

**Decision**: At boot (only when this feature is enabled), the bot calls
`GET /rest/api/3/issue/createmeta?projectKeys={projects}&expand=projects.issuetypes.fields`
once and inspects the result for the custom-field IDs (`customfield_10XXX`)
whose `name` matches `"Branch"` and `"Commit"` (case-insensitive). The
discovered IDs are cached for the daemon lifetime. Per-event Epic fetch
then uses `GET /rest/api/3/issue/{epic_key}?expand=names` and reads the
two cached custom-field IDs from the returned JSON.

**Rationale**:
- Hardcoding `customfield_10014` (a common Epic-Link default) is brittle
  across Jira tenants and over time.
- Discovery once at boot trades 1 extra Jira call for tenant-agnostic
  resilience. `getJiraIssueTypeMetaWithFields` is the documented surface.

**Fallback**: If discovery fails (the field doesn't exist, or its name
differs from `"Branch"`/`"Commit"`), the bot logs a clear startup error
and the trigger/handler refuse to start. Operator overrides via
`[handlers.jira_triage].branch_field_id` and `.commit_field_id` in
`config.toml`.

**Alternatives**:
- Hardcoded customfield IDs (rejected — brittle).
- Per-event lookup via `expand=names` and string match every time
  (rejected — wastes ~50 ms per event for a one-time lookup).

---

## R5. ssw-bundle clone strategy

**Decision**: Project-local clone at `<project_root>/var/ssw-bundle/`
(config knob `[handlers.jira_triage].ssw_bundle_path`, gitignored).
Initial clone uses `git clone --filter=blob:none --recurse-submodules=no
git@github.com:rebellions-sw/ssw-bundle.git`. Per-event prep:

```bash
# All inside a single per-clone advisory lock (flock):
cd <clone>
git fetch --prune origin                    # cheap with partial clone
git checkout --force <commit_sha>           # detached HEAD
git submodule update --init --recursive --depth 1  # shallow submodule fetch
```

**Rationale**:
- Partial clone (`--filter=blob:none`) reduces initial sync from many GB to
  a few hundred MB; per-checkout submodule init adds the needed blobs lazily.
- `git checkout --force <commit_sha>` puts the repo in detached HEAD, which
  the bot never tries to push from. Path guard in `infra/ssw_bundle.py`
  refuses to operate if the clone path resolves outside the project root
  (unless `allow_external_ssw_bundle=true`).
- Shallow submodule (`--depth 1`) is OK because the bot only reads files
  at that commit; no need for submodule history.
- Single lock (flock on `<clone>/.git/index`-adjacent file) serializes
  per-clone access. Since `concurrency=1`, this is mostly defensive.

**Why not `git show <commit>:<path>` instead of checkout**:
- `git show` works for super-repo files, but submodules require their own
  refs which the super-repo doesn't track without an init. Either way you
  end up doing a submodule fetch — at which point the simpler
  `git submodule update --init --recursive` keystroke wins.
- The persona will want to glob across product files (`grep`-style); a
  filesystem checkout makes that trivial. `git show` would require us to
  re-implement file listing on a tree object.

**Alternatives**:
- Fresh clone per ticket (rejected — many GB / network burst per event).
- Shared clone with the operator's `~/ssw-bundle/` (rejected — explicit
  spec FR-009, would mutate operator's working tree).
- Bare repo + worktrees (rejected — adds complexity for no win at
  concurrency=1).

**Cleanup**: No explicit prune. `--filter=blob:none` keeps the clone small;
`git gc --auto` runs occasionally as a side-effect of fetch. If the clone
ever grows beyond a configurable cap, operator manually deletes
`var/ssw-bundle/` and the next event re-clones.

---

## R6. Persona loader — share with pr_review or duplicate?

**Decision**: **Generalize**. Refactor `infra/pr_review_persona.py` →
`infra/persona_loader.py` with a `PersonaLoader` class that takes the
skill name as constructor arg. `pr_review` keeps using it with
`name="daeyeon-bot-code-review"`; `jira_triage` instantiates a second one
with `name="daeyeon-bot-jira-triage"`. The `Persona` dataclass moves from
`core/pr_review/persona.py` → `core/persona.py`.

**Rationale**:
- The persona-loading semantics (mtime-based hot-reload, frontmatter strip,
  body validation, repo-bundled-or-home fallback) are identical between
  pr_review and jira_triage. Duplicating the code is technical debt.
- The refactor is local: one file rename + import updates. Test coverage
  follows along.

**Scope guard**: The refactor lands in PR-1 (infra-only) alongside the new
Jira-specific files, so the diff is reviewable as one logical change.
Backwards compatibility for `pr_review` is preserved by re-exporting
`Persona` from `core/pr_review/__init__.py`.

**Alternatives**:
- Duplicate the loader (rejected — gets worse with every new persona
  handler).
- Generalize later, after this feature ships (rejected — every consumer
  added before refactor is another import to fix).

---

## R7. Loki query design

**Decision**: Use Loki's HTTP API `GET /loki/api/v1/query_range` at
`http://loki.ssw.rbln.in` (config `[loki].base_url`). No auth header
(spec confirmed cluster-internal, unauthenticated).

For each triage, the handler issues **up to four** queries in parallel:

```logql
# fwlog (requires hostname-as-IP)
{job="regression-fwlog", hostname="{host_ip}", test_name="{tc}"}

# smclog (requires hostname-as-IP)
{job="regression-smclog", hostname="{host_ip}", test_name="{tc}"}

# kernel (hostname-by-name)
{hostname="{host_name}", job=~"varlogs|systemd-journal", filename=~".*kern.*"}

# syslog (hostname-by-name)
{hostname="{host_name}", job=~"varlogs|systemd-journal", filename=~".*syslog.*"}
```

Time window: `start = ticket.start_ts` and `end = ticket.end_ts` if both
parsed cleanly; otherwise `start = ticket.created_at - 30 min` and
`end = ticket.created_at + 30 min` (FR-006 fallback).

Per-stream cap: `limit=5000` (Loki default). Per-stream byte cap enforced
client-side at `[loki].per_stream_max_bytes` (default 1 MB) — anything
longer is truncated and tagged `[truncated]` in evidence.

**Rationale**:
- Loki's `query_range` is the standard read endpoint. No auth keeps the
  client trivial.
- Parallel queries via `asyncio.gather` cut the wall-clock for four
  independent calls to ~max instead of sum.
- The kernel/syslog label schema (`job=~"varlogs|systemd-journal"`) is the
  promtail/vector default. If the cluster uses different labels, the
  config knob `[loki].kernel_query_template` overrides the LogQL.

**Edge handling**:
- 4xx (other than 429) on any stream → log warning, that stream's slice is
  empty, comment is tagged `[loki <stream>: unavailable]`. Does NOT fail
  the triage.
- 429 → backoff per `Retry-After`; second 429 → that stream's slice empty.
- 5xx on three consecutive retries (with exponential backoff) → stream
  slice empty, audit `loki_error` populated.

**Alternatives**:
- Loki MCP (`mcp__loki__loki_query`) (rejected for hot path — same
  reasoning as Jira MCP in R1: deterministic HTTP wins).
- Direct file reads on the test host (rejected — Loki streams give a
  query-able time window without parsing rsyslog/journal files manually).

---

## R8. SSH log dump strategy

**Decision**: Use `asyncssh` with password auth (`automation` /
`SSW_AUTOMATION_PASSWORD`). Connect once per host per triage; reuse the
connection across `sftp.get_attrs` (to list files) and `sftp.read_file`
(to fetch contents). `known_hosts` is at
`<state_dir>/jira_triage_known_hosts`, mode `accept-new` on first contact.

Per-triage flow:
1. `ssh_url = parse_from_body()` (FR-007). If absent → empty artifacts.
2. Open `SSHClientConnection` to `automation@<host>` (the host extracted
   from `ssh_url`).
3. Open SFTP subsystem. `listdir(remote_path)` to find candidate files.
4. For each candidate in `[handlers.jira_triage].ssh_fetch_globs` (default
   `["output.xml", "dmesg.log", "console.log"]`), if size ≤
   `ssh_max_file_bytes` (default 10 MB), fetch into memory. Oversized →
   skip with a note in evidence.
5. Close SFTP + connection.

**Rationale**:
- `asyncssh` is the established async SSH lib for Python. Built-in SFTP,
  pure-Python (vendors `cryptography` for the heavy lifting), and supports
  the `accept-new` host-key policy we need.
- Password auth is a known-bad shared lab credential — long-term plan is
  key auth. Until then, password is fine if scrubbed from logs (the
  literal "automation" string is added to the redaction regex set BEFORE
  any handler code lands — see Task T-002).
- 10 MB cap per file is generous for `output.xml` (typically ~1 MB) and
  small enough to prevent OOM from a runaway core dump.

**Edge handling**:
- Connection refused / timeout → empty artifacts + audit `ssh_error`.
- Auth fail → `AuthError` (which surfaces as DeadLetter, NOT daemon halt
  — SSH auth fail is per-host, not a daemon-wide credential problem).
- Path doesn't exist (run-id cleaned up) → `SFTPNoSuchFile` → empty
  artifacts + note in evidence "log dump expired".

**Alternatives**:
- Shell-out to `scp`/`rsync` (rejected — auth interactivity hard to
  manage; password through stdin needs `sshpass`, another dep).
- Open SSH connection pool across triages (rejected for concurrency=1;
  per-triage connection is simpler).
- HTTP file server in front of the dump dirs (rejected — would require
  ops work on every test host).

---

## R9. Hostname↔IP resolution

**Decision**: `socket.gethostbyname(name)` with a per-triage in-process
cache (`dict[str, str]`). Cache cleared at the start of each handler
invocation. Failure → fall back to `name` itself for kernel/syslog
queries; fwlog/smclog queries get skipped (they require IP).

**Rationale**:
- Internal DNS resolves SSW hostnames (confirmed 2026-05-13). Single
  syscall, ~ms latency, cached for the triage's duration.
- Per-triage cache (not daemon-lifetime) prevents stale IP if a host is
  re-imaged between triages.

**Alternatives**:
- Static map in config (rejected — adds operator burden, drifts).
- DNS cache with TTL across daemon lifetime (rejected — overengineered for
  ~1 lookup per host per triage).

---

## R10. Claude output structure & validation

**Decision**: Prompt Claude with persona body + appended JSON schema for
`TriageOutput`. Validate the response with Pydantic v2. On parse/validate
failure: one retry with the validation error appended; second failure →
`DeadLetter("claude returned malformed triage")`. Mirrors `pr_review`'s
ReviewOutput approach (commit `4c31a3` — schema injection into system
prompt).

```python
from typing import Literal
from pydantic import BaseModel, Field, model_validator

Domain = Literal["Driver", "SysFw", "CpFw", "SysSol", "DevOps", "Connectivity", "unknown"]
Severity = Literal["sev1", "sev2", "sev3", "unknown"]

class EvidenceItem(BaseModel):
    model_config = {"extra": "forbid"}
    source: str = Field(min_length=1, max_length=64)        # e.g. "loki.kernel", "ssh.dmesg", "test_code", "product_code"
    quote: str = Field(min_length=1, max_length=2000)
    citation: str = Field(min_length=1, max_length=512)     # e.g. "products/atom/fw/src/cmd_queue.c:412" or "2026-05-13T06:55:12.341Z"

class SuspectedDuplicate(BaseModel):
    model_config = {"extra": "forbid"}
    key: str = Field(pattern=r"^[A-Z]+-\d+$")
    basis: str = Field(min_length=1, max_length=512)

class TriageOutput(BaseModel):
    model_config = {"extra": "forbid"}
    summary_md: str = Field(min_length=1, max_length=16000)
    domain: Domain
    severity: Severity
    suspected_duplicates: list[SuspectedDuplicate] = Field(default_factory=list, max_length=5)
    needs_human: bool
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def evidence_required_when_concluded(self) -> "TriageOutput":
        if self.domain != "unknown" and not self.evidence:
            raise ValueError("evidence list is required when domain is concluded (FR-017)")
        return self
```

**Rationale**:
- Same precedent as pr_review (validated, audited).
- `extra="forbid"` blocks hallucinated keys.
- The `model_validator` enforces FR-017 structurally — a "Driver" verdict
  with no evidence list fails validation BEFORE posting.

**Alternatives**:
- Free-form Claude output with regex post-parse (rejected — same reasoning
  as pr_review).
- SDK structured-output API (rejected for now — same reasoning as pr_review).

---

## R11. Comment posting & wiki markup body

**Decision**: `POST /rest/api/2/issue/{key}/comment` with **Jira wiki
markup** body (plain string). The handler builds the markup
deterministically from `TriageDraft` using helpers in
`infra/jira_markup.py`. Wiki markup dialect matches the conventions
already in use in `ssw-bundle/inv/test_report/jira_markup.py`
(`*Branch*: …`, `h3. …`, `{noformat}…{noformat}`, `{{code}}`).

Example body:

```
h3. Symptom
rblnWaitJob TIMEDOUT 후 다음 잡 제출에서 {{kmd: [rbln-fwi] err_code=0x10007}} 추가 관측.

h3. Evidence cited
* loki.kernel @ 2026-05-13T06:55:12.341Z — {{rbln_drv: TDR detected on /dev/rbln0}}
* ssh.dmesg:1247 — {{atom_halt status: 6}}
* products/atom/fw/src/cmd_queue.c:412 — {{ERR_QUEUE_FULL}} 정의

h3. Likely layer
*CpFw* (command queue overflow — TDR은 증상)

h3. Next data to collect
* `dmesg | grep -A 50 atom_halt` from the affected host
* {{rblntrace}} of the same TC on a clean host to compare cmd-queue depth
```

**Rationale**:
- REST v2 comment endpoint accepts `body: str` (wiki markup); it is NOT
  deprecated, it's just the older surface that Atlassian keeps stable.
- Wiki markup is dramatically more concise than ADF for our fixed
  comment structure (4 sections, ~10 bullets). The 50–100 ADF nodes for
  one comment become ~30 lines of markup, easier to log, audit, and
  unit-test.
- `inv/test_report/jira_markup.py` already establishes the team's
  conventions; reusing the dialect makes the bot's comments visually
  consistent with manual triage output.
- Wiki markup builders are stdlib-only (string formatting); no third
  party Markdown-to-ADF converter.

**Force-supersede header** (mirroring pr_review):
The first line of a force-supersede comment is:
```
{quote}Updated triage (supersedes earlier bot comment posted at <HH:MM:SS UTC>).{quote}
```

A `{quote}…{quote}` block renders as a visually distinct callout in
Jira's UI.

**Alternatives**:
- REST v3 + ADF (rejected — verbose, no team precedent, costs nothing
  to keep v3 reads + v2 writes side-by-side under one httpx client).
- Use the `jira` Python library (rejected — it's synchronous; we'd need
  `asyncio.to_thread` around every call, defeating the async daemon.
  Direct httpx is cleaner.)
- Render Markdown → wiki markup via a converter (rejected — the bot's
  output schema is fixed, deterministic string concat is fine).

---

## R12. Self-supersede detection

**Decision**: Before posting, the handler queries
`jira_triage_audit WHERE issue_key=? ORDER BY id DESC LIMIT 1`. If a row
exists with `status='posted'`:
- `event.payload.force=False` → `Ack` with `audit.status='skipped_already_triaged'`. No comment.
- `event.payload.force=True` → post a new comment whose first ADF block
  is the supersede header (HH:MM:SS UTC = prior `posted_at`). New comment
  id is recorded; prior comment id is appended to the row's
  `superseded_comment_ids` JSON array via UPDATE.

**Rationale**:
- Jira REST v3 permits `DELETE /rest/api/3/issue/{key}/comment/{id}` for
  one's own comments. We deliberately do NOT use it (FR-024) — same logic
  as pr_review: chronological supersede is the only honest UX and the
  audit history stays clean.

**Alternatives**:
- Edit the prior comment in place via `PUT /rest/api/3/issue/{key}/comment/{id}`
  (rejected — Jira's edit notifications are noisy; the operator wants
  see-the-history behavior).
- Delete + recreate (rejected per FR-024 above).

---

## R13. Wall-clock budget enforcement

**Decision**: Handler wraps its `handle()` body in
`asyncio.wait_for(..., timeout=config.timeout_seconds)` (default 600 s).
On `TimeoutError`:
- Attempt 1 → raise `TransientError("triage exceeded {N}s budget")` →
  `Retry(default_backoff_s)`.
- Attempt 2 → raise `PermanentError("triage timed out twice")` → DeadLetter.

The 600 s budget covers:
- Jira fetch (~1 s)
- Epic fetch (~1 s)
- ssw-bundle git fetch + checkout + submodule init (~30–120 s, dominant
  on cache miss)
- Test-file grep (~1 s)
- Loki query × 4 in parallel (~5–10 s)
- SSH connect + SFTP fetch ~3 files (~5–10 s)
- Claude call (~30–120 s including thinking budget)
- ADF build + Jira POST (~1 s)
- Audit row insert (~1 ms)

Typical total: 60–300 s. Worst case (cold ssw-bundle, slow Loki, long
Claude call) bumps into the 600 s ceiling — that's the right point to
kick to retry.

**Rationale**:
- Per-event timeout protects the dispatcher from a single triage parking
  forever and starving the queue. Concurrency=1 makes this critical.
- Two-strikes rule before DeadLetter aligns with the daemon's existing
  Retry → DeadLetter ladder.

**NOT adding `timeout_s` to `HandlerManifest`**: this would be a
dispatcher-contract change affecting all handlers and CONTRACTS.md. Out
of scope for this feature. The handler self-enforces via `wait_for`.

**Alternatives**:
- Per-stage timeouts (rejected — explosion of knobs; the gross 600 s
  budget covers all stages adequately).
- No timeout (rejected — ssw-bundle hang or Loki hang would block the
  queue).

---

## R14. Logging, redaction, and metrics

**Decision**: Use the existing structlog setup. Every handler log
includes `event_id`, `trace_id`, `issue_key`, `parent_epic_key`,
`head_sha`, `run_id`, `comment_seq`, `status`. Jira request/response
bodies are NEVER logged in full — only HTTP status + size. Loki streams
are NEVER logged in full — only line counts per stream. SSH file
contents NEVER logged. Persona body NEVER logged.

The existing redaction processor (`infra/logging.py`) already scrubs
Slack / AWS / JWT / Anthropic OAuth / GitHub PAT patterns plus a
high-entropy fallback. Two literal additions land in PR-1 before
the handler ships:
- The literal string `"automation"` when it appears as `password` or
  `pwd` field value (rare in well-formed code, but cheap insurance).
- A regex for Jira API tokens (`ATATT[A-Za-z0-9_-]{40,}` — Atlassian's
  documented format).

No external metrics export in v1. Operators inspect via
`daeyeon-bot inspect status` + new `daeyeon-bot inspect jira-triage --issue
<key>` sub-command (mirrors `inspect pr-review`).

**Rationale**:
- Aligns with FR-022 (no secrets/paths in posted content) and the
  daemon's existing privacy-by-default posture.

---

## R15. Testing strategy

**Decision**:

- **Unit tests** use:
  - `httpx.MockTransport` for `JiraClient` and `LokiClient`.
  - A `FakeSshLogs` substitute for `infra/ssh_logs.py` returning canned
    SFTP-list / SFTP-read responses from a dict.
  - A tmp_path git fixture (super-repo with one fake submodule pointing
    at another tmp_path bare repo) for `SswBundleClient` tests.
  - `monkeypatch` for `socket.gethostbyname` in host-resolver tests.
  - `FakeClaudeSession` and `FakeClock` (already in `tests/fakes/`).
  - `FakePersonaLoader` (similar to pr_review's).

- **Integration tests** mount real `aiosqlite` against `tmp_path`, real
  migrations (including 005), real outbox/dispatcher, real git operations
  against the tmp_path fixture super-repo, and fakes for Jira/Loki/SSH/Claude.
  They exercise:
  - `jira_new_issue` trigger writes an event and outbox row in one tx.
  - Cursor advances atomically; replay of the same poll page is a no-op.
  - `jira_triage` handler walks all six stages end to end (fetch ticket →
    fetch Epic → checkout → grep test file → collect Loki + SSH → call
    Claude → post comment → audit).
  - Force-supersede produces a new comment and updates audit history.
  - Title regex miss → `skipped_not_regression_failure`.
  - Missing Epic field → `skipped_missing_metadata`.
  - Submodule init failure → `skipped_submodule_failure`.
  - Per-event timeout fires `Retry`.

- **No live Jira / Loki / SSH hits in CI**. A separate `just test-live`
  recipe (manual, off by default) runs against a real test SSWCI ticket
  for smoke validation.

**Coverage targets** (from `docs/PLAN.md` §6.3):
- new `core/jira_triage/` and `app/registry.py` additions ≥ 90%
- new `infra/jira_*.py`, `infra/loki.py`, `infra/ssh_logs.py`,
  `infra/ssw_bundle.py`, `infra/host_resolver.py` ≥ 80%
- new `cli/dev.py` + `cli/inspect.py` additions ≥ 60%

**Alternatives**:
- VCR-style fixtures of real Jira / Loki responses (rejected — they invite
  drift; the fakes are small enough to maintain and match the contracts).

---

## R16. Persona's relationship to oh-my-debugger (Option C hybrid)

**Decision (mirror of spec Clarification)**: The persona SKILL.md is
written in skill-delegation style — it describes when `/oh-my-debugger:short-triage`
SHOULD be invoked, but in PR-2 the SDK session is NOT configured to
actually allow Skill-tool invocations. The handler injects only a
prebuilt Run Snapshot as the user message; Claude analyzes from that
context alone. PR-4 (separate spec extension, not in this feature) will
flip the SDK options to enable Skill-tool calls.

This is captured in the persona body:

```markdown
## Stage 1 — context-only triage (current)
The handler has already collected: Loki streams, SSH artifacts, test
code at the run commit, relevant product code. Analyze from that. Do NOT
attempt to invoke external tools; the SDK session is locked.

## Stage 2 — skill-assisted triage (PR-4)
When the handler enables Skill-tool + Agent-tool invocations, you MAY
invoke /oh-my-debugger:triage as a cross-domain analysis pass when the
Stage 1 evidence is ambiguous between layers or a cascade is suspected.
The triage skill spawns multi-domain experts (kmd / fw / smc / umd /
wkld / tools) under a wave-based pipeline; it is heavier than
single-pass but matches the cross-domain nature of NPU regression
failures. Do NOT invoke /oh-my-debugger:short-triage — it's
single-pass like this persona and would just duplicate work.
```

**Why triage over short-triage for Stage 2**: An earlier draft of this
research recommended `short-triage` for "unattended automation =
single-pass" reasoning. daeyeon overruled (2026-05-13): regression
failures in this project regularly span KMD + FW + SMC, and the deeper
multi-expert pass is worth the wall-clock + token cost. The 600 s
handler budget (FR-031) covers Stage 1 comfortably; PR-4 (the spec
extension that enables Stage 2) will re-evaluate the budget — likely
bumping it to 1800 s or making it conditional on whether triage was
invoked.

**Rationale**:
- PR-2 is shippable on its own; the persona file can stay as-is when
  PR-4 lands (mtime hot-reload → instant rollout).
- Same SKILL.md serves both stages; no branching in code based on stage.

**Alternatives**:
- Two persona files, one per stage (rejected — adds operator confusion
  + ongoing duplication maintenance).
- Land Stage 2 in PR-2 alongside (rejected — bigger PR, harder review,
  SDK option change affects all handlers).

---

## R17. Repo-bundled persona default

**Decision**: Ship `daeyeon-bot/.claude/skills/daeyeon-bot-jira-triage/SKILL.md`
with the repo. PersonaLoader checks `~/.claude/skills/<name>/SKILL.md`
first; falls back to `<project_root>/.claude/skills/<name>/SKILL.md`.

**Rationale**:
- Operator can override locally if they tune the persona. The repo
  carries a stable default so a fresh checkout works out of the box.
- Same pattern as `daeyeon-bot-code-review` for pr_review.

**Validation**: The bundled SKILL.md is part of the CI lint set — pyright
+ a small test that asserts it parses (frontmatter + non-empty body) and
exceeds `min_persona_chars`.

---

## Summary of unresolved items

None. Every Technical Context placeholder was either resolved by spec
Clarifications or by one of R1–R17 above. Phase 0 gate passes.
