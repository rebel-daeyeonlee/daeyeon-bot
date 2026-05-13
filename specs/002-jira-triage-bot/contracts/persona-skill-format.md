# Contract — Persona SKILL.md format (jira_triage)

The persona is an operator-authored Claude Code skill at
`~/.claude/skills/<name>/SKILL.md` (with a repo-bundled fallback at
`<project_root>/.claude/skills/<name>/SKILL.md`). The bot reads it on
every triage event (with mtime-based caching) and uses the markdown
body as the triage system prompt.

**This contract is mostly identical to feature 001's
`persona-skill-format.md`**. The shared loader implementation
(`infra/persona_loader.py`, refactored from `infra/pr_review_persona.py`
per `research.md` R6) covers both. Differences specific to jira_triage
are called out below.

---

## 1. File location

```
~/.claude/skills/{name}/SKILL.md                                 # operator's home; highest priority
<project_root>/.claude/skills/{name}/SKILL.md                    # repo-bundled fallback
```

- `{name}` is the directory's basename, also the value the operator
  puts in `[handlers.jira_triage].persona_skill = "{name}"` in
  `config.toml`. Default: `"daeyeon-bot-jira-triage"`.
- Multiple variants live as sibling directories.
- Switching variants is a config edit + reload — no code change.

The bot resolves `~` against the daemon's HOME at boot. Symlinked
directories work; the bot stats the symlink target for mtime.

**Repo-bundled default**: `daeyeon-bot/.claude/skills/daeyeon-bot-jira-triage/SKILL.md`
ships with the repo. A fresh checkout has a working persona out of the
box (no operator setup required for first triage). The operator
override at `~/.claude/skills/daeyeon-bot-jira-triage/SKILL.md` wins
when present.

---

## 2. File format

```markdown
---
name: daeyeon-bot-jira-triage
description: daeyeon의 SSWCI regression-failure 자동 트리아지 페르소나. ...
allowed-tools: []        # Stage 1 (PR-2): no tool calls. Stage 2 (PR-4): Skill tool added.
---

# Role
...
# Operating principles
...
# Context shape
...
# Domain classification
...
# Output contract
...
# Hard rules
...
```

The file MAY have a YAML frontmatter. The frontmatter is
**parsed-but-ignored at runtime** — the bot does not key any behavior
off `name`, `description`, `allowed-tools`, etc. The frontmatter exists
so the same file is also a valid Claude Code skill the operator can
run interactively in their IDE.

The body — everything after the frontmatter (or the entire file if no
frontmatter) — is the triage system prompt verbatim. Markdown
structure inside the body is preserved.

---

## 3. Validation rules

The shared loader (`infra/persona_loader.py:PersonaLoader.load`)
validates:

| Check | Reason | Failure → |
|---|---|---|
| File exists at home OR repo-bundled path | persona configured but missing | `DeadLetter("persona unavailable: SKILL.md not found at <searched paths>")` |
| File readable | file mode prevents read | `DeadLetter("persona unavailable: cannot read SKILL.md (<errno>)")` |
| Body length ≥ `min_persona_chars` (default 200) after frontmatter strip | empty / accidentally-cleared persona | `DeadLetter("persona unavailable: body too short (<n> chars, min <min>)")` |
| Body has at least one non-whitespace line | placeholder content | `DeadLetter("persona unavailable: body is whitespace-only")` |

Any failure returns a `DeadLetter` from the handler — no generic
fallback comment is ever generated. Operator must repair the persona
and `daeyeon-bot ops replay --confirm` to resume.

---

## 4. Hot-reload semantics

Same as pr_review — stat the file every triage, compare `mtime_ns`,
re-read on change. Implementation lives in
`infra/persona_loader.py:PersonaLoader.load(name)` (refactored shared
loader). See `001/contracts/persona-skill-format.md` §4 for the
pseudocode.

---

## 5. Required body sections (specific to jira_triage)

Unlike pr_review's persona which has a single "review focus + voice"
shape, the jira_triage persona MUST include these sections to feed
the structured output contract:

| Section | Purpose |
|---|---|
| `# Role` | One paragraph stating the persona is for first-pass triage, not fix-it. |
| `# Operating principles` | The 5 evidence-first rules (cite, don't paraphrase; layer attribution before component; error propagation bottom→top; reproduction metadata gate; first-observation flag). |
| `# Context shape` | A description of the Run Snapshot JSON structure the handler injects — so Claude understands what's missing-vs-present in each section. |
| `# Domain classification` | The 6-domain ENUM (Driver / SysFw / CpFw / SysSol / DevOps / Connectivity) with keywords and source-path mapping, borrowed verbatim from `oh-my-debugger:short-triage` references. |
| `# Output contract` | Restates the JSON schema in prose (with examples) so Claude doesn't drift toward Markdown-only output. |
| `# Hard rules` | Anti-patterns to refuse: evidence-less conclusions, future tense, sympathetic tone, test-code repetition. |
| `# Language` | Korean prose + English technical terms preserved. |

The loader does NOT enforce these sections programmatically — it's a
documentation guideline. If the operator's persona omits one, Claude
may drift; the operator iterates on the persona, mtime-bumps it, next
triage uses the new version.

---

## 6. Stage 1 vs Stage 2 persona behavior

Per `research.md` R16, the persona is written for both stages of the
rollout. The body includes:

```markdown
## Stage 1 — context-only triage (current)
The handler has already collected: Loki streams, SSH artifacts, test
code at the run commit, relevant product code. Analyze from that. Do NOT
attempt to invoke external tools; the SDK session is locked.

## Stage 2 — skill-assisted triage (PR-4)
When the handler enables Skill-tool + Agent-tool invocations, you MAY
invoke /oh-my-debugger:triage for cross-domain analysis when Stage 1
evidence is ambiguous between layers or a cascade is suspected. Do NOT
invoke /oh-my-debugger:short-triage — single-pass duplicates the work
this persona already does.
```

PR-2 (this feature) ships with Stage 1 active. PR-4 (a separate
feature, not in this spec) flips the SDK options to enable Skill tool;
no persona edit is required — the persona already has Stage 2
instructions and Claude will use them once the harness allows.

---

## 7. What the persona body should and should NOT contain

### SHOULD

- Tone, evidence bar, language preference (Korean prose).
- Operating principles encoding daeyeon's debugging style.
- Domain classification rules.
- Example evidence citations the operator considers good.

### SHOULD NOT

- Operator's secrets, internal hostnames (other than the canonical
  `ssw-giga-*` family that are part of normal triage), customer names.
- Prompt-injection-style tricks like "ignore previous instructions" —
  they conflict with the appended JSON schema instructions.
- Direct ENUM additions outside the 6 canonical domains — the Pydantic
  schema rejects them anyway, and downstream tools (audit dashboards)
  rely on the ENUM being closed.
- Explicit log lines that you've seen — the persona is generic, the
  Run Snapshot is the per-event source of truth.

The bot does NOT enforce these "should not" items in code (it can't
reliably distinguish secrets from ordinary words in arbitrary persona
prose). The contract is documentary — operator's responsibility, with
the redaction processor as the safety net for output.

---

## 8. Example minimal valid SKILL.md

```markdown
---
name: daeyeon-bot-jira-triage
description: daeyeon의 SSWCI regression-failure 자동 트리아지 페르소나.
---

# Role
당신은 daeyeon-bot이 새 regression-failure 티켓에 다는 first-pass 트리아지
코멘트를 만든다. fix-it bot이 아니다. daeyeon이 출근해서 보면 "어느 layer
인지, 어떤 증거가 있는지, 다음에 뭘 모아야 하는지"가 이미 정리돼 있는 게
목표.

# Operating principles
1. Evidence before conclusion — file:line 또는 log line 인용 없이 root
   cause 주장 금지.
2. Layer attribution before component attribution — 먼저 어느 SW layer
   (Driver / SysFw / CpFw / SysSol / DevOps / Connectivity)인지 단정.
3. Error propagation 규칙: 가장 아래 layer가 root cause. HW fault →
   CpFw abort → Driver TDR → UMD ABORTED → App fail.
4. 재현 메타데이터(TC/host/start-end/branch+commit) 누락 → 진단 대신
   needs_human=true.
5. 이전 동일 시그니처 regression이 있으면 first-observation 아님 명시.

# Output contract
JSON object matching the TriageOutput schema injected by the handler.

# Language
summary_md는 한국어 산문 + 영어 기술어/경로/로그 라인 원문 유지.
```

This file is 200+ chars after frontmatter strip and has multiple
non-blank lines, so it passes the FR-007 sanity check. It is NOT a
complete production persona — see `daeyeon-bot/.claude/skills/daeyeon-bot-jira-triage/SKILL.md`
for the shipped version which includes the full Domain classification
table and Stage 1/Stage 2 split.

---

## 9. Cross-reference to feature 001

`daeyeon-bot-code-review` (pr_review's persona) is a sibling
persona. Both use the same loader (`infra/persona_loader.py`), the
same validation rules, and the same frontmatter-ignore semantics. The
operator typically authors both as separate skills in
`~/.claude/skills/`, with different bodies tuned for code review vs.
NPU regression triage. They never share frontmatter or body content.
