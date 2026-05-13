# Quickstart — Jira Regression-Failure Triage Bot

For an operator who already has the daeyeon-bot daemon running (Phases 0–7
complete, GitHub PR-review bot from feature 001 already shipped) and wants
to enable the Jira triage bot.

---

## 0. Prerequisites

```bash
# 1. The daemon is installed and the basic loop works.
just doctor                       # should pass
just status                       # should show the dispatcher running

# 2. Jira credentials are obtainable.
#    Generate an API token at:
#    https://id.atlassian.com/manage-profile/security/api-tokens

# 3. SSH access to test hosts as `automation` works from this machine.
ssh automation@ssw-giga-02 'echo ok'   # password: automation

# 4. Loki is reachable from this machine.
curl -fsS 'http://loki.ssw.rbln.in/ready'   # should print "ready"

# 5. ssw-bundle remote is reachable via SSH.
ssh -T git@github.com 2>&1 | head -3   # should NOT say "Permission denied"
```

If any of (3)–(5) fails, fix the network/credential prereq before enabling
the triage handler — the daemon will fail-fast on boot otherwise.

---

## 1. Author your triage persona (optional — a default ships)

The repo bundles a working default at
`<project_root>/.claude/skills/daeyeon-bot-jira-triage/SKILL.md`. The bot
uses this if you don't override.

To customize:

```bash
mkdir -p ~/.claude/skills/daeyeon-bot-jira-triage
cp <project_root>/.claude/skills/daeyeon-bot-jira-triage/SKILL.md \
   ~/.claude/skills/daeyeon-bot-jira-triage/SKILL.md
$EDITOR ~/.claude/skills/daeyeon-bot-jira-triage/SKILL.md
```

Minimum content (≥ 200 chars after frontmatter strip — the bot enforces
this):

```markdown
---
name: daeyeon-bot-jira-triage
description: daeyeon의 NPU regression-failure 트리아지 페르소나.
---

# Role
당신은 daeyeon-bot이 새 regression-failure 티켓에 다는 first-pass 트리아지
코멘트의 페르소나. fix-it bot이 아니다.

# Operating principles
... (see contracts/persona-skill-format.md §8 for the full minimal example)
```

Edit it whenever you want — the bot reads it fresh on each triage (no
restart needed). See `contracts/persona-skill-format.md` for the full
contract and a richer example.

---

## 2. Store the secrets

```bash
# Jira email (used as basic-auth username; matches the convention in
# ssw-bundle/inv/test_report/jira_client.py):
uv run daeyeon-bot setup-token jira-user
# (prompts for your Atlassian email; writes to macOS Keychain or 0600 file
#  depending on platform)

# Jira API token:
uv run daeyeon-bot setup-token jira-api-token
# (prompts for the token from id.atlassian.com)

# Shared SSH password for test hosts:
uv run daeyeon-bot setup-token ssw-automation-password
# (prompts; for now it's literally "automation" — long-term plan is key auth)
```

Verify all three are readable:
```bash
just doctor       # checks secrets provider for JIRA_USER, JIRA_API_TOKEN,
                  # SSW_AUTOMATION_PASSWORD, CLAUDE_CODE_OAUTH_TOKEN
```

---

## 3. Wire it into config

Edit `~/.daeyeon-bot/config.toml` (or wherever `DAEYEON_BOT_CONFIG` points):

```toml
[jira]
base_url = "https://rbln.atlassian.net/"
timeout_seconds = 30
# Leave empty for autodiscovery via getJiraIssueTypeMetaWithFields at boot.
issuetype_override = ""

[loki]
base_url = "http://loki.ssw.rbln.in"
per_stream_max_bytes = 1048576
timeout_seconds = 30

[triggers.jira_assigned]
enabled = true
poll_interval_seconds = 300              # 5 min — spec default
max_per_cycle = 200
team_name = "DevOps"                     # also match tickets assigned to this team; empty = assignee-only mode

[handlers.jira_triage]
enabled = true
idempotent = true
dedup_ttl_seconds = 86400
concurrency = 1
accepts = ["jira.assigned", "jira.triage.manual"]
allowed_projects = ["SSWCI"]             # start narrow; expand later
persona_skill = "daeyeon-bot-jira-triage"
min_persona_chars = 200
timeout_seconds = 600
ssw_bundle_path = "var/ssw-bundle"
allow_external_ssw_bundle = false
ssh_known_hosts_path = "jira_triage_known_hosts"
ssh_max_file_bytes = 10485760            # 10 MB per file
ssh_fetch_globs = ["output.xml", "dmesg.log", "console.log"]

[routing]
# Add these two lines without removing existing ones:
"jira.assigned"      = ["jira_triage"]
"jira.triage.manual"  = ["jira_triage"]
```

Apply migration #5 (adds `jira_assigned_state` and `jira_triage_audit`
tables) and reload config:

```bash
just migrate                       # idempotent; brings schema_version to 5
just doctor                        # should now show schema_version=5

# Restart the daemon to pick up the new config + trigger:
launchctl kickstart -k gui/$UID/com.daeyeon.bot   # macOS
# or:  systemctl --user restart daeyeon-bot       # Linux
```

---

## 4. First boot — what to expect

On the next daemon boot with these settings, you'll see (in
`~/.daeyeon-bot/launchd.out.log` or `journalctl --user -u daeyeon-bot`):

```jsonc
{"event": "jira.boot.probe_myself", "account_id": "557058:...", "email": "daeyeon.lee@..."}
{"event": "jira.boot.discover_fields", "project": "SSWCI", "branch_field": "customfield_10042", "commit_field": "customfield_10043", "issuetype": "Bug"}
{"event": "ssw_bundle.boot.path_ok", "path": "/.../daeyeon-bot/var/ssw-bundle"}
{"event": "ssh_logs.boot.known_hosts_ok", "path": "/.../daeyeon-bot/jira_triage_known_hosts"}
{"event": "trigger.start", "name": "jira_assigned", "poll_interval_seconds": 300}
```

If any boot probe fails:
- Wrong `JIRA_USER`/token → `AuthError` → daemon exits 78. Fix secrets,
  restart.
- ssw-bundle path outside project root → `ConfigError` → daemon exits 78.
  Fix `ssw_bundle_path`.
- Loki unreachable → warning logged but daemon continues (Loki failures
  are per-triage, not boot-fatal).

---

## 5. Smoke-test with a manual triage

Pick a recent SSWCI regression-failure ticket you have access to. Then:

```bash
# Dry run — fetches everything, runs Claude, prints the would-be comment
# but does NOT post:
uv run daeyeon-bot dev fire jira-triage --issue SSWCI-16787 --dry-run

# Real post:
uv run daeyeon-bot dev fire jira-triage --issue SSWCI-16787
```

Within ~5–10 minutes (depending on ssw-bundle checkout cold-cache, Loki
size, Claude latency) you should see the triage comment on the ticket:

```
h3. Symptom
rblnWaitJob TIMEDOUT 후 ...

h3. Evidence cited
* loki.kernel @ 2026-05-13T06:55:12.341Z — {{rbln_drv: TDR detected on /dev/rbln0}}
* ssh.dmesg:1247 — {{atom_halt status: 6}}
...

h3. Likely layer
*CpFw* (command queue overflow — TDR은 증상)

h3. Next data to collect
* `dmesg | grep -A 50 atom_halt` from the affected host
* {{rblntrace}} of the same TC on a clean host
```

Inspect what the bot did:

```bash
uv run daeyeon-bot inspect jira-triage --issue SSWCI-16787
```

Outputs the `jira_triage_audit` row: status, comment_id, posted_at,
domain, severity, evidence_count, loki/ssh error fields if any, persona
name + mtime_ns at triage time.

---

## 6. Verify the auto-trigger

Have a collaborator assign an SSWCI regression-failure ticket to you (or
to the DevOps team), or carefully assign one yourself:

```bash
# Watch the structured log for trigger + handler events:
tail -f ~/.daeyeon-bot/launchd.out.log | \
  jq 'select(.event | startswith("jira_assigned.") or startswith("jira_triage."))'
```

Within ~10 minutes of the assignment (one polling cycle + handler
budget) the trigger emits the event and the handler posts the comment.

**Note on cold-start**: on the very first poll after enabling the
trigger, the bot seeds state for every ticket currently in your watched
queue but does NOT triage any of them retroactively. This is intentional
(see FR-004a). To force-triage a pre-existing ticket, use the manual
command from §5 with `--force`.

---

## 7. Force a re-triage on an already-triaged ticket

When you want a fresh pass without filing a new ticket:

```bash
uv run daeyeon-bot dev fire jira-triage --issue SSWCI-16787 --force
```

The new comment is prepended with a `{quote}…{quote}` callout:

```
{quote}Updated triage (supersedes earlier bot comment posted at 14:30:11 UTC).{quote}

h3. Symptom
...
```

The prior comment remains in ticket history (the bot does NOT delete
its own comments; chronological supersede is the only mode).

---

## 8. Pause / resume

The existing daemon kill-switch applies — when paused, no Jira
comments post:

```bash
uv run daeyeon-bot lifecycle pause   --reason "ooo for the day"
uv run daeyeon-bot lifecycle resume
```

Pending triage events stay queued during the pause; they all process
after resume (no duplicates, no losses — see SC-007).

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `daeyeon-bot run` exits 78 right after enabling jira_triage | Secrets unreadable (Keychain locked, 0600 file missing, env path) | `daeyeon-bot ops doctor`; re-run `setup-token` for the failing key |
| `daeyeon-bot run` exits 78 with `JIRA_USER does not match emailAddress` | `JIRA_USER` value is the wrong email | Re-run `setup-token jira-user` with the correct email |
| Auto-trigger fires but the audit shows `skipped_not_regression_failure` | Ticket title doesn't match the `regression-test . <host> . <tc>` regex | Expected — the bot only triages regression-failure shaped tickets. If the title is misformatted, fix the ticket title or skip. |
| Audit shows `skipped_missing_metadata` with `missing_fields=["branch","commit"]` | Parent Epic has empty Branch / Commit custom fields | Backfill the Epic fields and retry: `dev fire jira-triage --issue X --force` |
| Audit shows `skipped_unresolvable_commit` | The Epic's commit SHA isn't on `origin` (force-pushed, lost) | Verify the SHA exists: `cd var/ssw-bundle && git fetch origin && git cat-file -e <sha>`. If genuinely lost, fix the Epic or skip. |
| Audit shows `skipped_submodule_failure` | One of the submodules can't be fetched (network, key, GC) | Inspect `audit.error` for the failing path; fix submodule access; retry with `--force` |
| Comment posts but Evidence section says `[loki <stream>: unavailable]` | Loki was unreachable or slow during triage | One-off transient — next triage should be clean. Persistent → check `[loki].base_url` and network. |
| Comment posts but Evidence section says `[ssh: auth_failed]` | `SSW_AUTOMATION_PASSWORD` is stale | Re-run `setup-token ssw-automation-password` |
| Triage takes ~10 min on first event after a long idle period | Cold `var/ssw-bundle/` cache — `git fetch` pulls a lot | Normal. Subsequent triages on the same branch reuse the local objects and take seconds. |
| Persona edits not reflected in next triage | mtime didn't bump (editor wrote in place, second-resolution mtime) | `touch ~/.claude/skills/daeyeon-bot-jira-triage/SKILL.md` to bump mtime, then trigger again |
| Comment doesn't render bullets — shows raw markdown | wiki-markup builder bug or the persona returned literal `*` instead of bullet markup | Inspect the audit row's `summary_md` to see what Claude returned; fix the persona to be clearer about output format, retry |
| `daeyeon-bot` runs but Loki section in evidence is consistently empty | DNS or label mismatch | `socket.gethostbyname("ssw-giga-02")` works? If not, fix DNS. Loki labels diverge from `regression-fwlog`/`regression-smclog`? Update `[loki].kernel_query_template` and friends. |

---

## 10. What still happens automatically

Everything the existing daemon already provides:

- **At-least-once delivery** — every triage event runs to completion or
  ends in `dead_letter` for operator inspection.
- **Crash recovery** — interrupted triages are re-attempted on next
  boot (the handler is `idempotent=True`, so the audit-row check
  prevents duplicate comments).
- **Heartbeat self-alert** — if the polling task hangs, the heartbeat
  emits `heartbeat.tick_lag` to launchd-stderr / journald.
- **Hot SQLite backup** — `jira_triage_audit` history is backed up by
  `just backup` along with the rest of `state.db`.
- **Events retention** — audit rows older than 90 days (default) prune
  alongside the events they reference.
- **Redaction** — `JIRA_API_TOKEN`, `SSW_AUTOMATION_PASSWORD`, and
  Atlassian token patterns (`ATATT[A-Za-z0-9_-]{40,}`) are added to
  the structlog redaction set BEFORE this feature ships. Verify in
  `infra/logging.py:_REDACTION_PATTERNS`.

---

## 11. Disabling

To turn off auto-triage temporarily without losing in-flight events:

```toml
[triggers.jira_assigned]
enabled = false
```

Plus restart. The handler stays enabled so manual triages
(`dev fire jira-triage`) still work — useful for one-off operator runs
while the auto path is paused.

To fully disable both:

```toml
[triggers.jira_assigned]
enabled = false

[handlers.jira_triage]
enabled = false
```

The migrations and audit history stay intact; re-enabling is just a
config edit + restart.
