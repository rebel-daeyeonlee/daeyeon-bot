# Contract — Jira REST API surface used by the bot

This file is **the only place** that lists which Jira REST endpoints the
bot calls. If a call doesn't appear here, it doesn't happen. FR-018
("never modify ticket fields") is enforced by code review against this
enumeration: any new write endpoint must be added here first.

All calls are made via `httpx.AsyncClient` against
`https://rbln.atlassian.net/` with **basic auth**
`(JIRA_USER, JIRA_API_TOKEN)` and `Accept: application/json`. All JSON
shapes below show only the fields the bot reads/writes — extra fields
returned by Jira are ignored.

---

## Auth & secret keys

Mirrors the convention already in use in
`ssw-bundle/inv/test_report/jira_client.py`:

- `JIRA_USER` — operator's Atlassian email (e.g. `daeyeon.lee@rebellions.ai`).
  Stored under this key in the daemon's secrets provider chain (keychain
  → 0600 file → env w/ `--insecure-env`).
- `JIRA_API_TOKEN` — token from
  `https://id.atlassian.com/manage-profile/security/api-tokens`. Same
  secrets provider chain.

`httpx.BasicAuth(JIRA_USER, JIRA_API_TOKEN)` is wired in
`infra/jira_client.py:__init__`. No Bearer header.

**Why basic auth, not Bearer**: the team's existing tooling
(`inv/test_report/jira_client.py`) uses `basic_auth=(user, token)` with
the `jira` Python library. Aligning lets the operator reuse the same
token across tooling and reduces mental load when rotating.

---

## REST version mix

- **REST v3** for search and issue-get (more accurate field metadata, JSON
  ADF description). Used by the polling trigger and the handler's
  metadata fetch.
- **REST v2** for posting comments — accepts a plain-string `body` in
  Jira wiki markup, which is more concise to generate than ADF and
  matches the wiki-markup conventions already in use in
  `inv/test_report/jira_markup.py` (`*bold*`, `h3.`, `{noformat}`,
  `{quote}`, etc.).

Both versions live under the same `httpx.AsyncClient` instance with the
same basic auth. The mix is documented here and enforced in code by
keeping the URL prefixes in named constants.

---

## Endpoints used (6 total — 5 read, 1 write)

### 1. `GET /rest/api/3/myself` — auth probe (boot only)

```
GET /rest/api/3/myself
```

**Read shape (subset)**:
```jsonc
{
  "accountId": "557058:abcdef0-1234-…",
  "emailAddress": "daeyeon.lee@rebellions.ai",
  "displayName": "daeyeon"
}
```

Run once at boot when this feature is enabled. A non-2xx response raises
`AuthError` ⇒ daemon halts (exit 78). The accountId is cached for the
daemon lifetime and used in audit rows (NOT in posted comment bodies).
The probe also validates that `JIRA_USER` matches `emailAddress` (mismatch
⇒ `AuthError("JIRA_USER does not match Jira-reported emailAddress")`).

### 2. `GET /rest/api/3/issue/createmeta` — field discovery (boot only)

```
GET /rest/api/3/issue/createmeta?projectKeys=SSWCI&expand=projects.issuetypes.fields
```

**Read shape (subset)**:
```jsonc
{
  "projects": [
    {
      "key": "SSWCI",
      "issuetypes": [
        {
          "name": "Bug",
          "fields": {
            "customfield_10042": {
              "name": "Branch",
              "schema": { "type": "string" }
            },
            "customfield_10043": {
              "name": "Commit",
              "schema": { "type": "string" }
            }
          }
        }
      ]
    }
  ]
}
```

Run once at boot. Caches:
- `branch_field_id` (e.g. `customfield_10042`) — discovered by matching field `name="Branch"`
- `commit_field_id` (e.g. `customfield_10043`) — discovered by matching field `name="Commit"`
- `team_field_id` (e.g. `customfield_10050`) — discovered by matching field `name="Team"` (Jira Atlassian Teams). Bot uses this id in JQL when `team_name` is non-empty; otherwise the team clause is omitted.

If `team_name` is set but the Team field can't be discovered, the daemon
fails-fast at boot with `ConfigError("team_name=DevOps configured but no
Team field in Jira project schema; set team_field_id explicitly or
clear team_name")`.

Operator may override any via `config.toml` (`[handlers.jira_triage].branch_field_id`,
`.commit_field_id`, `.team_field_id`).

### 3. `GET /rest/api/3/search` — JQL polling

```
GET /rest/api/3/search
   ?jql=(assignee = currentUser() OR "Team" = "DevOps")
        AND project IN ("SSWCI")
        AND summary ~ "regression-test"
        AND status != Closed
   &fields=key,summary,issuetype,assignee,parent,status,<team_field_id>
   &maxResults=50
   &startAt=0
```

The bot's JQL admits tickets via TWO assignment paths:
- `assignee = currentUser()` — directly assigned to daeyeon (resolved via `/myself` at boot).
- `"Team" = "DevOps"` — Jira Atlassian Teams field. The exact field
  reference (`"Team"`, `cf[NNNNN]`, or the team's UUID) depends on the
  Jira tenant; see endpoint #2 for boot-time discovery.

The trigger then re-reads each returned issue's `assignee` and team field
to determine `assignee_path ∈ {"user","team"}` for audit purposes; JQL
itself doesn't tell us which clause matched.

**Read shape (subset)**:
```jsonc
{
  "startAt": 0,
  "maxResults": 50,
  "total": 1,
  "issues": [
    {
      "key": "SSWCI-16787",
      "fields": {
        "summary": "regression-test . ssw-giga-02 . TC-0033-Dram_test_with_exception",
        "created": "2026-05-13T06:54:48.924+0000",
        "issuetype": { "name": "Bug" },
        "parent":    { "key": "SSWCI-16784" }     // Epic parent (set on bug creation; see jira_bug.py:137)
      }
    }
  ]
}
```

The JQL **adds `summary ~ "regression-test"`** to pre-filter to
regression-failure tickets at the server side — the bot's title regex
is the canonical filter, but the JQL fuzzy match keeps the polling page
size small even when other Bugs land in the same time window.

Used by the polling trigger. The trigger paginates by re-issuing with
`startAt += maxResults` until fewer than `maxResults` are returned, or
the `[triggers.jira_new_issue].max_per_cycle` safety cap is hit.

### 4. `GET /rest/api/3/issue/{key}?expand=names,renderedFields` — ticket meta

```
GET /rest/api/3/issue/SSWCI-16787?expand=names,renderedFields
```

**Read shape (subset)**:
```jsonc
{
  "key": "SSWCI-16787",
  "fields": {
    "summary": "regression-test . ssw-giga-02 . TC-0033-Dram_test_with_exception",
    "created": "2026-05-13T06:54:48.924+0000",
    "reporter": { "accountId": "..." },
    "status":   { "name": "Open" },
    "parent":   { "key": "SSWCI-16784" },     // Epic key (per inv/test_report/jira_bug.py:137 "parent": {"key": epic_key})
    "description": "...",                      // Wiki markup string (REST v3 returns the source string; renderedFields gives HTML)
    "labels":   ["regression", "release/v3.2"]
  },
  "renderedFields": {
    "description": "<html rendering — used as fallback when wiki-markup parse fails>"
  }
}
```

Used by the handler for ticket body parsing (Start/End timestamps, SSH
URL, error-log excerpt). The handler regex-parses the wiki-markup
`description` directly — wiki markup is line-oriented and the
expected fields (`*Start*:`, `*End*:`, `ssh://...`) appear as plain text.

**Note on description format**: ssw-bundle authoring code
(`inv/test_report/jira_bug.py:177-185`) generates wiki-markup bodies
like:
```
*Branch*: release/v3.2
*Host*: ssw-giga-02
*Suite*: ...
*Affected testcases*: 3
*Report*: https://...
h3. Suite Setup Skip Reason
{noformat}
<stack trace>
{noformat}
```

The handler's body parser is tolerant of this exact shape (line-oriented
key extraction + a `{noformat}` block extractor for the error log
excerpt).

### 5. `GET /rest/api/3/issue/{epic_key}?expand=names` — Epic field fetch

```
GET /rest/api/3/issue/SSWCI-16784?expand=names
```

**Read shape (subset)**:
```jsonc
{
  "key": "SSWCI-16784",
  "fields": {
    "summary": "...",
    "issuetype": { "name": "Epic" },
    "customfield_10042": "release/v3.2",          // Branch (id from #2 cache)
    "customfield_10043": "140112e9203598c72f568501eecac706cc125dcf"  // Commit
  }
}
```

If either custom field is empty/null, the handler records
`missing_fields=["branch"]` / `["commit"]` / `["branch","commit"]` in the
audit row and skips with `status='skipped_missing_metadata'`.

Some Epics may also carry `branch`/`commit` in the wiki-markup
description (e.g. `*Branch*: release/v3.2` as a line) instead of (or in
addition to) the custom fields. The handler tries custom fields first,
falls back to a description regex, and records which source supplied
each field in audit metadata.

### 6. `POST /rest/api/2/issue/{key}/comment` — post triage comment (ONLY write)

```
POST /rest/api/2/issue/SSWCI-16787/comment
Content-Type: application/json

{
  "body": "h3. Symptom\n...\nh3. Evidence cited\n* loki.kernel @ ... — {{rbln_drv: TDR detected ...}}\n* ssh.dmesg:1247 — {{atom_halt status: 6}}\n..."
}
```

**Write body**: Jira wiki markup as a plain string. The bot constructs
it from `TriageDraft` using helpers in `infra/jira_markup.py`:
- (optional) supersede header line
- `h3. Symptom\n<one sentence>`
- `h3. Evidence cited\n* <source> @ <citation> — {{<quote>}}\n...`
- `h3. Likely layer\n<domain ENUM> — <short justification>`
- `h3. Next data to collect\n* <bullet>\n...`

Wiki-markup helpers reuse the conventions already documented in
`inv/test_report/jira_markup.py` (`{noformat}`, `{quote}`, `{{code}}`,
`*bold*`). Long quotes go inside `{noformat}` blocks so they render
without escape worries.

**Read shape on success (subset)**:
```jsonc
{
  "id": "10001",
  "created": "2026-05-13T07:15:02.123+0000",
  "author":  { "accountId": "557058:..." },
  "self":    "https://rbln.atlassian.net/rest/api/2/issue/SSWCI-16787/comment/10001"
}
```

The bot records `id`, `created` (as `posted_at`), and `self` URL in
`jira_triage_audit`.

#### Constants (NOT parameters)

- The wrapper accepts `body` only as `str` (wiki markup). The wrapper
  refuses empty strings (TypeError) and refuses bodies above 32 KB
  (Atlassian's documented comment cap).
- The wrapper never sets `visibility` — comments are visible to all
  ticket viewers.
- The wrapper does NOT mutate any field on the ticket via `properties`
  or any other side channel.

---

## Endpoints we do NOT call

Any non-trivial write endpoint is forbidden. The following are explicitly
**banned** for the bot per FR-018. Any addition requires a spec amendment
and a new entry in this file.

| Endpoint | Why banned |
|---|---|
| `PUT /rest/api/3/issue/{key}` | Would modify summary / description / fields. |
| `POST /rest/api/3/issue` | We never create new tickets — that's `inv/test_report/jira_bug.py`'s job. |
| `POST /rest/api/3/issue/{key}/transitions` | Status transitions are out of bounds. |
| `PUT /rest/api/3/issue/{key}/assignee` | Assignee changes are out of bounds. |
| `POST /rest/api/3/issueLink` | We don't create Jira links — suspected duplicates are mentioned in `evidence` only. |
| `POST /rest/api/3/issue/{key}/worklog` | We never log time. |
| `DELETE /rest/api/2/issue/{key}/comment/{id}` | The API permits deleting our own comments, but we use chronological supersede (FR-024). |
| `PUT /rest/api/2/issue/{key}/comment/{id}` | We do NOT edit prior comments — supersede instead. |
| `POST /rest/api/3/issue/{key}/votes` | Voting is noise. |
| `POST /rest/api/3/issue/{key}/watchers` | Subscribing is the operator's job. |
| `POST /rest/api/3/issue/{key}/properties/{key}` | We never write structured side-channel data on tickets. |

---

## Auth & rate-limit error contract

`httpx` raises on transport errors; the wrapper inspects status codes:

- `HTTP 401` ⇒ raise `core.errors.AuthError`. Dispatcher halts; CLI
  exits 78. Operator rotates `JIRA_API_TOKEN`.
- `HTTP 403` ⇒ raise `core.errors.AuthError` (Atlassian Cloud returns
  403 for both wrong-creds and missing-permission; in our single-tenant
  case both mean "fix the token / grant access").
- `HTTP 429` ⇒ raise `RateLimitError(retry_after=<value from Retry-After header>)`.
  Dispatcher schedules `Retry(retry_after)`; trigger backs off.
- `HTTP 404` on `GET issue/{key}` ⇒ raise `PermanentError("Jira issue not
  found or no access: {key}")` ⇒ `DeadLetter`.
- `HTTP 400` on `POST comment` ⇒ raise `PermanentError` (body malformed —
  that's a logic bug in the wiki-markup builder; DeadLetter with the
  Jira-returned `errors` payload).
- `HTTP 5xx` ⇒ raise `TransientError` ⇒ `Retry(default_backoff_s)`.
- `httpx.ConnectError` / `httpx.ReadTimeout` ⇒ `TransientError`.

Trigger-side 401 also halts the daemon (the polling task can't silently
spin while broken).

---

## Wrapper API (`infra/jira_client.py`)

```python
class JiraClient:
    def __init__(
        self,
        *,
        base_url: str,        # "https://rbln.atlassian.net/"
        user: str,            # JIRA_USER (email)
        token: str,           # JIRA_API_TOKEN
        timeout_s: float,
        http: httpx.AsyncClient,
    ): ...

    async def myself(self) -> JiraIdentity: ...
    async def discover_fields(self, project_keys: list[str]) -> FieldDiscovery: ...
    async def search_jql(
        self, *, jql: str, fields: list[str], start_at: int, max_results: int
    ) -> SearchPage: ...
    async def issue_get(
        self, key: str, *, expand: list[str] | None = None
    ) -> IssueDetail: ...
    async def post_comment(
        self, key: str, *, body_wiki: str
    ) -> PostedComment: ...
```

All methods are pure I/O; wiki-markup building, regex parsing, and
dedup logic live in the handler and `infra/jira_markup.py` (not here).
The wrapper's only job is to translate HTTP errors into the daemon's
exception taxonomy and parse responses.

The wrapper holds `httpx.BasicAuth(user, token)` internally and applies
it to every request. The `token` value never lands in `os.environ`,
log lines, or audit rows — the structlog redaction processor's literal
patterns extend to include the token at boot (the redaction processor
is given the in-memory string once and patterns it).
