# Contract: OnCall Wiki surface (`infra/oncall_wiki.py`)

Read-only consumer of `rebellions-sw/ssw-devops-oncall` (Obsidian vault).

## Pinned vault-relative paths (single source of truth)

`infra/oncall_wiki.py` references these as module constants; the corrupt-clone
sentinel, the `search()` scope, and the docs all use the same strings.

```
INCIDENTS_DIR          = "wiki/oncall/incidents"      # *.md canonicals (signature: frontmatter)
RECOVERY_PLAYBOOK_PATH = "wiki/notes/recovery-playbook.md"   # always-included first-response index + clone sentinel
```

## Pinned `domain:` vocabulary (drift guard)

The incident frontmatter `domain:` field vocabulary, read from the live vault
(2026-06-19). `TriageOutput.owner_area` MUST equal this set (+ `Unknown`).
`test_ci_triage_schemas.py::test_owner_area_matches_pinned_vault_domain_vocab`
asserts the Pydantic `Literal` against this pinned list (hermetic — it does not
hit the live vault).

```
DOMAIN_VOCABULARY = ["DevOps", "SysFw", "SysSol", "Connectivity", "Driver", "HW"]
```

**Re-pin trigger** (runbook): re-pin this list whenever an incident file
introduces a new `domain:` value, or on any oncall-vault schema change. A real
vault drift silently mis-routes attribution (undermining SC-005) until re-pinned.

## Methods

- `ensure_fresh() -> WikiRefresh` — clone-if-absent (or re-clone a partial/corrupt
  clone) then `git pull --ff-only`. Never raises on a transient git failure;
  returns `WikiRefresh(available, stale, error)`. No commit/push/reset method
  exists (read-only).
- `ls_remote()` — boot reachability probe (`git ls-remote --quiet`). Raises
  `AuthError`/`ConfigError` → exit 78 at boot.
- `search(signatures, phrases) -> list[WikiMatch]` — signature-first keyword scan
  over `INCIDENTS_DIR`, always including `RECOVERY_PLAYBOOK_PATH`. Multi-word
  phrase matched against a `signature:` frontmatter line scores highest; body
  match lower; lone token lowest. Pure-Python scan (the vault is small; avoids a
  hard `rg` PATH dependency in headless deploys — same approach as
  `ssw_bundle.grep_test_case`).

## Headless-safe git env

Every git invocation sets `GIT_TERMINAL_PROMPT=0` and a `GIT_SSH_COMMAND` with
`BatchMode=yes`, `StrictHostKeyChecking=accept-new`, and a managed 0600
`UserKnownHostsFile` (the 0600 perm-check is reused from `ssh_logs.py:74-86`).
The `accept-new` pinning is achievable here because this adapter is git-over-
OpenSSH (not asyncssh as in `ssh_logs.py`, which fell back to `known_hosts=None`).

## Two-layer path guard

1. Hard-ban `~/ssw-devops-oncall` (the operator's working vault) regardless of
   `allow_external`.
2. Require `clone_path` inside `project_root` unless `allow_external=true`.
Default `clone_path = var/ssw-devops-oncall/` (gitignored, fully rebuildable).
