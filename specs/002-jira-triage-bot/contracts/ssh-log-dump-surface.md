# Contract — SSH log-dump access surface

This file pins how the bot reaches the test hosts' log-dump directories
to fetch RF artifacts (output.xml, dmesg captures, console logs) that
Loki does not carry.

All SSH I/O via `asyncssh`. Credentials are shared-lab:
- user: `automation`
- password: from secrets provider chain under key `SSW_AUTOMATION_PASSWORD`

The literal password value is added to the structlog redaction patterns
in `infra/logging.py` BEFORE any handler that calls this surface lands.

---

## Path layout (confirmed 2026-05-13)

```
ssh://automation@<hostname>:/mnt/data/logs/regression-test/<run-id>/<hostname>/<TC>/
```

Example:
```
ssh://automation@ssw-giga-02:/mnt/data/logs/regression-test/25746526668-1/ssw-giga-02/TC-0033-Dram_test_with_exception/
```

Components:
- `<hostname>`: extracted from ticket title regex (FR-008). Also appears
  in the SSH URL path twice (once as the SSH host, once as a path
  segment) — they MUST match. Mismatch ⇒ audit `ssh_error="hostname_mismatch"`.
- `<run-id>`: format `<digits>-<digits>` (e.g. `25746526668-1`).
  Extracted from the SSH URL regex (FR-007).
- `<TC>`: same TC name from the title (e.g. `TC-0033-Dram_test_with_exception`).

The SSH URL is parsed from the ticket body via:
```python
SSH_URL_RE = re.compile(
    r"ssh://automation@(?P<host>[\w.-]+):"
    r"(?P<path>/mnt/data/logs/regression-test/"
    r"(?P<run_id>[\d\-]+)/"
    r"(?P<host2>[\w.-]+)/"
    r"(?P<tc>TC-\d+-\S+))"
)
```

---

## What we fetch

The handler lists the remote directory via SFTP and tries each glob
from `[handlers.jira_triage].ssh_fetch_globs` (default
`["output.xml", "dmesg.log", "console.log"]`). For each match:

| File | Use |
|---|---|
| `output.xml` | Robot Framework run output. Parsed (later) for FAIL message + tags + setup/teardown trace. v1 sends the full file as `ssh.output_xml` evidence channel. |
| `dmesg.log` | Captured kernel ring buffer at end-of-test. Complements Loki kernel stream. |
| `console.log` | Captured serial console (if present). Critical for FW boot/abort cases that don't reach the kernel logger. |

Each file is fetched if its size is ≤ `[handlers.jira_triage].ssh_max_file_bytes`
(default 10 MB). Oversized files are NOT fetched; the handler records a
note in evidence: `[ssh.<file>: oversized — {N} MB, cap {M} MB]`.

If a file in `ssh_fetch_globs` is not present in the remote directory,
the handler silently skips it (it's an optional file).

The bot **never** reads `*.tar` / `*.tar.gz` / `*.zip` archives in v1 —
they're too easy to be huge and the unpack budget is unbounded. A
follow-up may add selective archive listing.

---

## Known-hosts policy

`asyncssh` is configured with `known_hosts=<state_dir>/jira_triage_known_hosts`
and **policy `accept-new`**. First contact with a new test host writes a
known-hosts entry; subsequent contacts validate strictly. A host-key
change raises `asyncssh.HostKeyNotVerifiable` ⇒ audit
`ssh_error="host_key_changed:<host>"` + the triage continues without SSH
artifacts. The operator inspects and (after verifying the change was
legitimate, e.g., host re-imaged) deletes the stale entry from the
known-hosts file.

The known-hosts file mode is `0o600`; the loader refuses to start if
the file exists with looser permissions.

---

## Auth flow

```python
async with asyncssh.connect(
    host=ssh_host,
    username="automation",
    password=SSW_AUTOMATION_PASSWORD,             # from secrets provider
    known_hosts=str(known_hosts_path),
    server_host_key_algs=["ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"],
    keepalive_interval=30,
    connect_timeout=10,
) as conn:
    async with conn.start_sftp_client() as sftp:
        ...
```

On `asyncssh.PermissionDenied` (wrong password) → raise `AuthError` —
but **scoped to this SSH host**, not daemon-wide. The handler catches
it, records `ssh_error="auth_failed"`, and proceeds with empty SSH
artifacts. The operator gets paged via the audit; this is NOT a daemon
halt because Jira/Loki/Claude are independent.

(Daemon-wide `AuthError` semantics are reserved for the OAuth/Claude/Jira
auth path; SSH auth is a per-host credential.)

---

## Error contract

| Exception | Audit field | Handler outcome |
|---|---|---|
| `asyncssh.ConnectionLost` / `asyncio.TimeoutError` | `ssh_error="connect_failed"` | proceed without SSH artifacts |
| `asyncssh.HostKeyNotVerifiable` | `ssh_error="host_key_changed:<host>"` | proceed without SSH artifacts |
| `asyncssh.PermissionDenied` | `ssh_error="auth_failed"` | proceed without SSH artifacts |
| `asyncssh.sftp.SFTPNoSuchFile` on listdir | `ssh_error="path_not_found:<remote_path>"` | proceed without SSH artifacts |
| `asyncssh.sftp.SFTPNoSuchFile` on individual file | (per-file note; no audit error) | skip that file, continue listing |
| File size > cap | (per-file note in evidence) | skip oversized file, continue |
| `asyncssh.SFTPError` (other) | `ssh_error="sftp:<code>"` | proceed without SSH artifacts |

SSH outage **never** fails the triage outright — Loki + ticket body +
ssw-bundle source can carry the load. Audit row makes the gap visible
to the operator.

---

## Wrapper API (`infra/ssh_logs.py`)

```python
class SshLogClient:
    def __init__(
        self,
        *,
        username: str,
        password: str,
        known_hosts_path: Path,
        max_file_bytes: int,
        connect_timeout_s: float,
    ): ...

    async def fetch_directory(
        self,
        *,
        host: str,
        remote_path: str,
        globs: list[str],
    ) -> SshFetchResult: ...
```

`SshFetchResult` shape:
```python
@dataclass(frozen=True, slots=True)
class SshFetchResult:
    artifacts: tuple[SshArtifact, ...]    # successfully fetched
    skipped:   tuple[SshSkip, ...]        # filename + reason ("oversized" / "not_found")
    error:     str | None                 # populated when the whole fetch failed (connect/auth/path)

@dataclass(frozen=True, slots=True)
class SshSkip:
    filename: str
    reason:   str
    detail:   str
```

The wrapper is single-purpose — connect once, list, fetch matching files,
close. No streaming, no recursion, no archive extraction.

---

## Operations we do NOT perform

| Operation | Why banned |
|---|---|
| Write to remote (`put`, `rename`, `remove`, `chmod`) | The bot is read-only on test hosts. |
| Execute remote commands (`run` / `create_session`) | We do NOT shell out on test hosts. SFTP only. |
| Open shell session for an interactive operator hand-off | Out of scope. |
| Recurse into subdirectories | v1 reads only the run directory's top level. Recursing into per-iteration subdirs is a follow-up. |
| Open archive files | Out of scope per "what we fetch". |
| Reuse SSH connections across triages | concurrency=1; per-triage connection is simpler than a pool. |

Any of these would require a spec amendment and a new contract entry.

---

## Long-term action item

Spec FR-021 acknowledges the shared `automation` password is a weakness.
The follow-up plan (tracked in RUNBOOK after this feature ships):
1. Generate an SSH key pair for the bot (`~/.daeyeon-bot/ssh/id_ed25519`).
2. Distribute the public key to all SSW test hosts under
   `automation`'s `~/.ssh/authorized_keys`.
3. Flip `[handlers.jira_triage]` to prefer key auth; password becomes
   fallback then disabled.

This is a follow-up rollout, NOT a v1 feature.
