# Contract — Claude triage output format

The bot uses Claude as a structured-output triage engine. The contract
here is **the format Claude must emit** so the handler can validate it
with Pydantic and forward it to Jira's REST API as a wiki-markup comment.

---

## 1. The Pydantic v2 schema (single source of truth)

```python
from typing import Literal
from pydantic import BaseModel, Field, model_validator

Domain   = Literal["Driver", "SysFw", "CpFw", "SysSol", "DevOps", "Connectivity", "unknown"]
Severity = Literal["sev1", "sev2", "sev3", "unknown"]

class EvidenceItem(BaseModel):
    model_config = {"extra": "forbid"}
    source:   str = Field(min_length=1, max_length=64)        # "loki.fwlog" | "loki.smclog" | "loki.kernel" | "loki.syslog" | "ssh.output_xml" | "ssh.dmesg" | "ssh.console" | "test_code" | "product_code"
    quote:    str = Field(min_length=1, max_length=2000)
    citation: str = Field(min_length=1, max_length=512)       # "file:line" or ISO8601 timestamp or "ssh.<filename>:<line>"

class SuspectedDuplicate(BaseModel):
    model_config = {"extra": "forbid"}
    key:   str = Field(pattern=r"^[A-Z]+-\d+$")
    basis: str = Field(min_length=1, max_length=512)

class TriageOutput(BaseModel):
    model_config = {"extra": "forbid"}

    summary_md:           str = Field(min_length=1, max_length=16000)
    domain:               Domain
    severity:             Severity
    suspected_duplicates: list[SuspectedDuplicate] = Field(default_factory=list, max_length=5)
    needs_human:          bool
    evidence:             list[EvidenceItem]       = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def evidence_required_when_concluded(self) -> "TriageOutput":
        # FR-017: never diagnose without cited evidence
        if self.domain != "unknown" and not self.evidence:
            raise ValueError("evidence list is required when domain != 'unknown'")
        return self
```

`extra = "forbid"` blocks hallucinated keys (`"approve": true`,
`"resolved": true`, etc.). A malformed-extra-key response triggers the
retry-then-DeadLetter ladder.

---

## 2. The system prompt assembled by the handler

```text
{persona_body}

---

You are triaging the regression-failure ticket below. Output ONLY a JSON
object that matches this exact JSON schema. No prose before or after, no
Markdown code fence — just the JSON object on stdout.

JSON schema:
{
  "type": "object",
  "additionalProperties": false,
  "required": ["summary_md", "domain", "severity", "needs_human"],
  "properties": {
    "summary_md": {
      "type": "string", "minLength": 1, "maxLength": 16000,
      "description": "Four-section Korean prose with English technical terms preserved verbatim: Symptom / Evidence cited / Likely layer / Next data to collect. Use Markdown headings (###) and bullets. The handler will convert this to Jira wiki markup."
    },
    "domain": {
      "type": "string",
      "enum": ["Driver", "SysFw", "CpFw", "SysSol", "DevOps", "Connectivity", "unknown"]
    },
    "severity": {
      "type": "string",
      "enum": ["sev1", "sev2", "sev3", "unknown"]
    },
    "suspected_duplicates": {
      "type": "array", "maxItems": 5,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["key", "basis"],
        "properties": {
          "key":   { "type": "string", "pattern": "^[A-Z]+-\\d+$" },
          "basis": { "type": "string", "minLength": 1, "maxLength": 512 }
        }
      }
    },
    "needs_human": { "type": "boolean" },
    "evidence": {
      "type": "array", "maxItems": 50,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["source", "quote", "citation"],
        "properties": {
          "source":   { "type": "string", "enum": ["loki.fwlog","loki.smclog","loki.kernel","loki.syslog","ssh.output_xml","ssh.dmesg","ssh.console","test_code","product_code"] },
          "quote":    { "type": "string", "minLength": 1, "maxLength": 2000 },
          "citation": { "type": "string", "minLength": 1, "maxLength": 512 }
        }
      }
    }
  }
}

Rules:
- `evidence` MUST be non-empty whenever `domain` is concluded (i.e.
  `domain != "unknown"`).
- Every `evidence.quote` MUST appear verbatim in the corresponding
  Run Snapshot section (loki_streams[<stream>], ssh_artifacts[<file>],
  test_code, product_code[<path>]). Do not paraphrase.
- `evidence.citation` MUST be in one of these formats:
  - For Loki streams: ISO8601 timestamp UTC (e.g. "2026-05-13T06:55:12.341Z")
  - For SSH artifacts: `ssh.<filename>:<line>` (e.g. "ssh.dmesg:1247")
  - For source files: `<repo-relative path>:<line>` (e.g. "products/atom/fw/src/cmd_queue.c:412")
- `severity` SHOULD be `sev1` only when the log contains a hard signal
  (panic / abort / data corruption / hardware halt). Otherwise prefer
  `sev2` or `sev3`. Use `unknown` when you cannot tell.
- `needs_human` MUST be `true` when:
  - Required metadata was missing in the Run Snapshot, OR
  - The evidence is ambiguous between two domains, OR
  - The conclusion requires a hypothesis you cannot ground in the
    provided context.
- Never echo content that looks like a secret (API keys, tokens,
  passwords, private keys). If you spot one in the diff, mention it
  abstractly in `summary_md`; do not paste the value.
```

The user message passed to Claude is the rendered Run Snapshot:

```text
=== Ticket ===
Key: {issue_key}
Title: {title}
Reporter: {reporter}

=== Run meta ===
Hostname: {hostname}  (IP: {host_ip})
Run ID: {run_id}
Start: {start_ts}    End: {end_ts}   (fallback: {fallback_bool})
Branch: {branch}    Commit: {commit}
Epic: {epic_key}

=== Error log (from ticket body) ===
{error_log_excerpt}

=== Test code: {test_file_path} ===
{test_code or "(not located in suites tree)"}

=== Product code excerpts ===
[products/common/kmd/...:NNN]
{excerpt}
[products/atom/fw/...:MMM]
{excerpt}
...

=== Loki streams ===
[loki.fwlog]  ({n} lines, truncated: {truncated})
<line 1>
<line 2>
...
[loki.smclog] ...
[loki.kernel] ...
[loki.syslog] ...

=== SSH artifacts ===
[ssh.output_xml] ({n} bytes)
{contents}
[ssh.dmesg] ...
[ssh.console] ...

=== Collection errors ===
loki: {loki_error or "ok"}
ssh:  {ssh_error or "ok"}
```

If any section is empty (collection failed or the channel had no data),
the snapshot includes the section heading with `(empty)` underneath. The
persona is told (via the system prompt) not to invent content for empty
sections.

---

## 3. Validation pipeline (`handlers/jira_triage.py`)

```
claude_response_text
   │
   ▼
JSON parse  ─── fail ──► retry once with parse-error appended ─── fail again ──► DeadLetter
   │
   ▼
Pydantic TriageOutput.model_validate  ─── fail ──► retry once with errors() appended ─── fail again ──► DeadLetter
   │
   ▼
_verify_evidence_quotes(evidence, snapshot)
   │  ├─ every quote appears verbatim in the cited source?  pass
   │  └─ any quote not found?                                raise PermanentError("fabricated evidence quote")
   ▼
_redact(summary_md, evidence)               # reuse infra/logging.py redaction regexes
   │  └─ match? raise PermanentError("redaction would alter posted content")
   ▼
infra/jira_markup.py: build_comment(triage_output, *, supersede_header=None) → wiki_markup_body: str
   │
   ▼
jira_client.post_comment(issue_key, body_wiki=...)
   │
   ▼
on success:  audit row INSERT (status='posted', comment_id, posted_at); Ack
on 422 from Jira: raise PermanentError (logic bug in markup builder) → DeadLetter
on 5xx / 429:     Retry(...)
```

`_verify_evidence_quotes` is critical — it prevents the persona from
fabricating log lines that the bot can't actually trace back to a real
source. The check is exact-substring (Python `in`) against the
corresponding snapshot section.

`_redact` runs the structlog redaction regex set
(`infra/logging.py:_REDACTION_PATTERNS`) over `summary_md` and every
evidence `quote`. Any match raises `PermanentError("redaction would
alter posted content")` rather than silently mutating Claude's output —
mirrors `pr_review`'s strict-redaction policy (SC-008 at 100%).

---

## 4. Force-supersede header

When the handler is processing a manual `--force` event whose audit
history shows a prior posted comment for the same `issue_key`, it
**prepends** this exact block to the wiki-markup body (NOT to
`summary_md`):

```
{quote}Updated triage (supersedes earlier bot comment posted at <HH:MM:SS UTC>).{quote}

```

`<HH:MM:SS UTC>` is `posted_at` of the prior comment formatted in UTC.
The `{quote}…{quote}` block renders as a callout in Jira's UI.

---

## 5. "Missing metadata" Summary template (no Claude call)

When `Epic.branch` or `Epic.commit` is missing (FR-005), the handler
SKIPS Claude entirely and writes only an audit row with
`status='skipped_missing_metadata'`. **No comment is posted** in this
case — distinct from pr_review's "too large" template which does post.
The rationale: a comment that says "I can't triage this because the
Epic has no branch" doesn't help the operator any more than the audit
row does, and adds noise on the ticket.

(If operator preference shifts to "always post something so I see the
bot tried", that's a one-line change in `_handle_missing_metadata` —
deferred.)

---

## 6. "Title regex miss" handling

When the title doesn't match the regression-failure regex (FR-002,
FR-004), the handler also SKIPS Claude AND skips the comment.
`status='skipped_not_regression_failure'`. Same rationale as §5.

---

## 7. Claude call configuration

- Model: existing `[claude].model` (default `claude-opus-4-7-[1m]`).
- Thinking budget: NOT enabled in v1. The triage decision is largely
  pattern-matching against the structured snapshot; extended thinking
  is an opt-in for a follow-up if quality demands it.
- Max output tokens: capped at 8000 (≈ 6 KB of JSON, fits comfortably
  under the 16 KB `summary_md` cap).
- Retries: the bot's TWO retries (parse failure + validate failure) are
  separate from Claude SDK's own retries; the SDK's transient errors
  surface as `TransientError` and route to dispatcher Retry.

---

## 8. Output language contract

`summary_md` is **Korean prose with English technical terms / paths /
log lines preserved verbatim**. Examples of preservation:

- Korean: `"rblnWaitJob TIMEDOUT 후 다음 잡 제출에서..."`
- English-preserved: `"kmd: [rbln-fwi] err_code=0x10007"`,
  `"products/atom/fw/src/cmd_queue.c:412"`,
  `"2026-05-13T06:55:12.341Z"`

The persona enforces this in its body. The handler does NOT
post-process language — what Claude returns goes through markup
conversion and out to Jira as-is (after redaction).

A success-criteria check (SC-012) audits posted comments for the
presence of Korean characters; comments with zero Korean characters
trigger an operator alert (likely a persona regression).
