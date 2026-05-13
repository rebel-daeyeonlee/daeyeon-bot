# Contract — ssw-bundle checkout surface

This file pins the git operations the bot performs against the
ssw-bundle super-repo and its submodules to reproduce a regression run's
source state.

The bot maintains **one dedicated clone** at
`<project_root>/var/ssw-bundle/` (config knob
`[handlers.jira_triage].ssw_bundle_path`). The bot MUST NEVER touch the
operator's working tree (`~/ssw-bundle/`).

---

## Operations the bot performs (4 total)

### 1. `git clone` (initial only)

```bash
git clone --filter=blob:none \
          git@github.com:rebellions-sw/ssw-bundle.git \
          <ssw_bundle_path>
```

Run by `infra/ssw_bundle.py:ensure_clone()` when `<ssw_bundle_path>` is
empty or doesn't contain a `.git` directory.

- `--filter=blob:none` is a partial clone — pulls the trees + commits
  but defers blob fetches to checkout time. Initial sync ~500 MB
  instead of the full multi-GB super-repo.
- `--recurse-submodules=no` is implicit (clone does NOT auto-init
  submodules; the bot inits them lazily per checkout).
- The remote is hardcoded to `git@github.com:rebellions-sw/ssw-bundle.git`.
  Override via `[handlers.jira_triage].ssw_bundle_remote` if the operator
  needs to point at a fork (out of scope for v1).

The clone uses the operator's existing SSH key (`~/.ssh/id_*`). The bot
does NOT manage a separate deploy key in v1.

### 2. `git fetch` (per triage)

```bash
cd <ssw_bundle_path>
git fetch --prune --filter=blob:none origin
```

Run by `infra/ssw_bundle.py:ensure_checkout(branch, commit)` at the
start of each triage. `--prune` removes deleted branches; `--filter`
keeps the partial-clone semantics.

### 3. `git checkout` (per triage)

```bash
git checkout --force --detach <commit_sha>
```

- `--force` overwrites local files if any drifted (the bot is the only
  writer; this is defensive).
- `--detach` puts the repo in detached HEAD. There is no branch to
  accidentally push to.
- `<commit_sha>` is the 40-hex from the parent Epic's `Commit` custom
  field. The bot validates it matches `^[0-9a-f]{40}$` BEFORE running
  git.

If the commit isn't reachable after fetch (e.g., force-pushed and lost,
or never on origin), `git checkout` exits non-zero; the wrapper raises
`UnresolvableCommitError` which the handler maps to `audit.status =
'skipped_unresolvable_commit'`.

### 4. `git submodule update` (per triage)

```bash
git submodule update --init --recursive --depth 1
```

- `--init` initializes any submodule not yet registered locally.
- `--recursive` walks nested submodules (none currently, but cheap
  insurance).
- `--depth 1` is a shallow submodule fetch — only the commit pinned by
  the super-repo, not the submodule's history. Saves bandwidth and disk.

If any submodule fails to init (network error, key rejected, commit
GC'd from the submodule's remote), the command exits non-zero. The
wrapper captures stderr, parses out the offending submodule path, and
raises `SubmoduleInitError(failed_paths=[...])`. The handler maps this
to `audit.status = 'skipped_submodule_failure'` with `missing_fields`
populated from the failed paths.

---

## Operations we do NOT perform

| Operation | Why banned |
|---|---|
| `git push` (any variant) | The bot is read-only on the remote. Detached HEAD makes accidental pushes hard but the wrapper has no push method to call. |
| `git commit` | Likewise. The wrapper has no commit method. |
| `git reset --hard <ref>` outside the checkout step | The checkout step IS the reset. Standalone reset is not exposed. |
| `git clean -fdx` | The bot doesn't generate untracked files in normal operation. If the operator deletes something stray, that's a manual recovery. |
| `git branch` / `git switch` to a named branch | We always work in detached HEAD. No branch to switch to. |
| `git remote add` / `set-url` | The single hardcoded remote is the contract. |
| Submodule URL rewrites | We use whatever `.gitmodules` says, as-is. |
| Removing submodules | Detected as orphaned submodules in the super-repo? Out of scope; the operator handles repo-cleanliness issues. |
| GPG sign / verify | Not relevant — the bot never commits, and we trust the operator's HTTPS/SSH transport. |

Any of these would require a spec amendment and a new entry in this file.

---

## Path guards (`infra/ssw_bundle.py`)

The wrapper rejects ANY of the following at construction time:

1. `ssw_bundle_path` resolves outside `project_root` (via
   `Path(ssw_bundle_path).resolve().is_relative_to(project_root)`)
   UNLESS `allow_external_ssw_bundle = true` is set explicitly. The
   wrapper raises `ConfigError("ssw_bundle_path is outside project root;
   set allow_external_ssw_bundle=true to override")`.
2. `ssw_bundle_path` resolves to `~/ssw-bundle/` (the operator's
   working tree) regardless of the `allow_external_ssw_bundle` flag —
   this is hard-banned to prevent accidents.
3. `ssw_bundle_path` points at a directory the bot cannot write to.
4. `ssw_bundle_path` contains a `.git/config` with `[remote "origin"]
   url = <anything other than the configured remote>` — the wrapper
   refuses to operate on an unfamiliar clone.

These checks run on `infra/ssw_bundle.py` construction (boot time when
the feature is enabled), so the daemon fails fast at startup, not
mid-event.

---

## Concurrency

A single `asyncio.Lock` protects the clone for the duration of any
`ensure_checkout()` call. Since `concurrency=1` on the handler, two
triages will never overlap in practice — but the lock makes the
operation safe even if someone enables higher concurrency in the
future.

---

## Wrapper API (`infra/ssw_bundle.py`)

```python
class SswBundleClient:
    def __init__(
        self,
        *,
        clone_path: Path,
        remote_url: str,
        project_root: Path,
        allow_external: bool,
    ):
        # Path guards run here. Raises ConfigError on violation.
        ...

    async def ensure_clone(self) -> None:
        """Run initial clone if .git is absent. Idempotent."""
        ...

    async def ensure_checkout(
        self,
        *,
        branch: str,                  # informational only; commit_sha is the source of truth
        commit_sha: str,              # 40-hex; raises if invalid
    ) -> None:
        """Fetch origin, checkout commit (detached), init submodules. Raises
        UnresolvableCommitError or SubmoduleInitError on failure."""
        ...

    async def read_file(self, relative_path: str) -> str | None:
        """Read a single file at the current checkout. None if missing.
        Refuses to read outside the clone via Path.resolve() check."""
        ...

    async def grep_test_case(self, *, tc_name: str) -> Path | None:
        """Search test/system/suites/**/*.robot for a `Test Case` block
        whose name == tc_name. Returns the relative path or None.
        Implementation: stream-grep through ripgrep or Python re."""
        ...
```

The wrapper exposes only these methods. There is no `push`, no `commit`,
no shell-out for arbitrary git commands. The handler's product-code
collection runs through `read_file()` and `grep_test_case()`.

---

## Test fixture (`tests/fakes/ssw_bundle.py` or shared helper)

For unit tests, the wrapper is exercised against a `tmp_path` fixture:

```python
@pytest.fixture
def ssw_bundle_fixture(tmp_path: Path) -> Path:
    """Build a minimal git fixture mimicking ssw-bundle:
    - super-repo at <tmp_path>/super (init + 2 commits + 1 fake submodule)
    - submodule at  <tmp_path>/sub   (init + 1 commit)
    - super-repo's .gitmodules points at <tmp_path>/sub
    Returns the super-repo path; tests run SswBundleClient against it.
    """
```

The fixture is small (<100 lines) and reused across all
`ssw_bundle`-touching tests. No network access required for CI.

---

## Cleanup policy

No automatic cleanup of `var/ssw-bundle/`. The clone is reused across
all triages. If it ever grows beyond an operator-perceived comfort
level, the operator manually `rm -rf var/ssw-bundle/` and the next
triage will re-clone.

Out of scope for v1:
- Periodic `git gc` (relies on git's built-in `auto-gc`).
- Automatic prune when disk usage exceeds a cap.
- LRU eviction across multiple clones (we only have one).
