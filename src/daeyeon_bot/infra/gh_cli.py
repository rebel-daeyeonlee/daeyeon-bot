"""Async wrapper around the operator's local `gh` CLI.

All GitHub access flows through this module. Auth is delegated to `gh` —
the daemon stores no GitHub token of its own. The 5 endpoints exposed here
are the entire GitHub surface (`contracts/github-api-surface.md`).

Error mapping (per `contracts/github-api-surface.md` §"Auth & rate-limit"):
    HTTP 401 / auth failure  → AuthError       (daemon halts, exit 78)
    HTTP 403 + rate headers  → RateLimitError  (Retry with rate-limit backoff)
    HTTP 422 on POST         → PermanentError  (DeadLetter; local validator bug)
    HTTP 404 on GET          → PermanentError  (PR not found / no access)
    Other 5xx / timeout      → TransientError  (Retry default backoff)
    HTTP 200 on probe         → success (parsed JSON returned)

No retries inside the wrapper; the dispatcher handles them.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from daeyeon_bot.core.errors import (
    AuthError,
    PermanentError,
    RateLimitError,
    RunLogUnavailableError,
    TransientError,
)

_DEFAULT_TIMEOUT = 30.0
# GitHub Review API `event` values — see `/repos/.../pulls/.../reviews` docs.
# `APPROVE` counts toward branch protection. `REQUEST_CHANGES` blocks merge —
# we don't expose it here (too strong a signal for an automated bot). The
# handler picks between `APPROVE` (0 findings) and `COMMENT` (any finding).
ReviewEvent = Literal["APPROVE", "COMMENT"]

# stderr patterns. `gh` writes "HTTP <code>" or "gh: <msg> (HTTP <code>)" depending
# on the subcommand; cover both. Auth-failure phrasing varies across `gh` versions.
_HTTP_CODE_RE = re.compile(r"HTTP\s+(\d{3})")
_AUTH_PHRASES = (
    "authentication failed",
    "authentication required",
    "bad credentials",
    "could not refresh",
    "must authenticate",
    "no logged-in account",
    "token has not been granted",
)
_RATE_LIMIT_PHRASES = (
    "api rate limit exceeded",
    "x-ratelimit-remaining: 0",
)
# `gh run view --log-failed` stderr phrases that mean the log is GONE FOR GOOD
# (not a transient blip): run deleted, never existed, or logs aged out of
# GitHub's retention / overwritten by a re-run. Mapped to RunLogUnavailableError
# → skip (Ack), NOT Retry — re-queueing can never recover the log. The existing
# `gh api`-shaped `_raise_error` would mis-map these (no HTTP code) to
# TransientError → infinite Retry. See specs/003-ci-monitor-bot/plan.md.
_RUN_LOG_UNAVAILABLE_PHRASES = (
    "could not find any workflow run",
    "could not find any run",
    "no logs found",
    "could not find logs",
    "log not found",
    "logs expired",
    "has expired",
    "no longer available",
    "could not find the run",
    "run not found",
    "blobnotfound",
    "the specified blob does not exist",
)


def _is_http_5xx(message: str) -> bool:
    """True if `message` mentions an HTTP 5xx code (e.g. a TransientError text)."""
    match = _HTTP_CODE_RE.search(message)
    return match is not None and 500 <= int(match.group(1)) < 600


def _opt_str(value: object) -> str | None:
    """Return `value` if it's a non-empty str, else None."""
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class _GhResult:
    """Outcome of one `gh` subprocess invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class FailedJob:
    """A failed job of a workflow run, with the data needed to resolve a Loki
    host + window. `runner_name` is the runner (may be a controller distinct from
    the DUT); `started_at`/`completed_at` are ISO8601 (or None)."""

    id: str
    name: str
    runner_name: str | None
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class WorkflowRunMeta:
    """Identity of one run, enough to find its sibling runs (feature 003 P1
    cross-run comparison). `workflow_id` keys the per-workflow runs listing;
    `head_sha`/`head_branch` distinguish this PR's attempts from other PRs'."""

    run_id: str
    workflow_id: str | None
    head_sha: str | None
    head_branch: str | None
    event: str | None
    run_attempt: int | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    """A completed run of a workflow (feature 003 P1). Run-level only — we do not
    fetch per-job conclusions here (that would be N extra calls); the run
    `conclusion` is the comparison signal, mirroring how on-call eyeballs
    'are other PRs also failing this workflow?'."""

    id: str
    head_sha: str | None
    head_branch: str | None
    status: str | None  # queued | in_progress | completed
    conclusion: str | None  # success | failure | cancelled | timed_out | ...
    event: str | None
    created_at: str | None


class GhCli:
    """Thin async wrapper around `gh api` for the 5 GitHub endpoints we use."""

    def __init__(self, *, timeout_seconds: float = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout_seconds

    # ── Public surface ────────────────────────────────────────────────────

    async def auth_status(self) -> None:
        """Probe `gh auth status`. Raises AuthError if not logged in."""
        result = await self._run("auth", "status")
        if result.returncode != 0:
            raise AuthError("gh auth status failed: " + _safe_decode(result.stderr).strip())

    async def auth_user(self) -> str:
        """Return the authenticated user's `login`. One call at boot."""
        payload = await self._api("GET", "/user")
        login = payload.get("login")
        if not isinstance(login, str) or not login:
            raise PermanentError("gh api /user returned no login")
        return login

    async def search_review_requested(
        self, username: str, *, extra_query: str = ""
    ) -> list[dict[str, Any]]:
        """Search open PRs awaiting review by `username`.

        `extra_query` is appended verbatim to the base query — used by the
        trigger to inject a repo allowlist (`(repo:a/b OR user:c)`) that
        narrows traffic at the GitHub side instead of relying on a
        client-side filter alone. Empty string keeps the legacy behavior.

        Returns the flattened `items` list from `GET /search/issues`.
        """
        if not username:
            raise PermanentError("github.username is empty; cannot build search query")
        query = f"is:open is:pr review-requested:{username} archived:false"
        if extra_query:
            query = f"{query} {extra_query}"
        payload = await self._api(
            "GET",
            "/search/issues",
            extra=("-f", f"q={query}", "-f", "per_page=100"),
            paginate=True,
        )
        if isinstance(payload, dict):
            items_raw = payload.get("items", [])
            if isinstance(items_raw, list):
                return [item for item in items_raw if isinstance(item, dict)]
        return []

    async def search_authored(
        self, username: str, *, extra_query: str = ""
    ) -> list[dict[str, Any]]:
        """Search open PRs authored by `username`.

        Used by the trigger when `[handlers.pr_review].review_self = true` so
        the operator's own PRs get reviewed. `extra_query` carries the same
        repo-allowlist narrowing as `search_review_requested`. The two searches
        are disjoint — GitHub never lists you as a reviewer of your own PR — so
        the trigger can union the results without de-duping by author.

        Returns the flattened `items` list from `GET /search/issues`.
        """
        if not username:
            raise PermanentError("github.username is empty; cannot build search query")
        query = f"is:open is:pr author:{username} archived:false"
        if extra_query:
            query = f"{query} {extra_query}"
        payload = await self._api(
            "GET",
            "/search/issues",
            extra=("-f", f"q={query}", "-f", "per_page=100"),
            paginate=True,
        )
        if isinstance(payload, dict):
            items_raw = payload.get("items", [])
            if isinstance(items_raw, list):
                return [item for item in items_raw if isinstance(item, dict)]
        return []

    async def run_view_log_failed(self, repo: str, run_id: str) -> str:
        """Return the failed-job logs of a GitHub Actions run (read-only).

        `gh run view <run_id> --repo <repo> --log-failed`. This is NOT a
        `gh api` call, so it does NOT use `_raise_error` (which is HTTP-code
        shaped and would map a gone/expired log — emitted as human-readable
        stderr with no HTTP code — to TransientError → infinite Retry). A
        permanently-unavailable log raises `RunLogUnavailableError` (→ skip),
        transient failures raise `TransientError` (→ Retry). See
        specs/003-ci-monitor-bot/plan.md §gh_cli.py extension.
        """
        if not repo or not run_id:
            raise PermanentError("run_view_log_failed requires repo and run_id")
        result = await self._run("run", "view", run_id, "--repo", repo, "--log-failed")
        if result.returncode != 0:
            self._raise_run_log_error(repo, run_id, result)
        return _safe_decode(result.stdout)

    async def run_failed_job_logs(self, repo: str, run_id: str) -> str:
        """Fetch logs for each FAILED job of a run via the per-job logs API,
        tolerating individual missing blobs (404 / BlobNotFound).

        More robust than `run_view_log_failed`: `gh run view --log-failed` bails
        ENTIRELY when one nested/reusable/matrix job's log blob is absent (common
        for healthcheck runs), whereas the per-job API (the same endpoint the web
        UI's job page uses) serves every job whose blob still exists. Raises
        `RunLogUnavailableError` only when NO failed job's log is retrievable.
        Falls back to `run_view_log_failed` when the run reports no failed jobs.
        """
        if not repo or not run_id:
            raise PermanentError("run_failed_job_logs requires repo and run_id")
        failed = await self._failed_jobs(repo, run_id)
        if not failed:
            return await self.run_view_log_failed(repo, run_id)
        chunks: list[str] = []
        for job in failed:
            text = await self._job_logs(repo, job.id)
            if text:
                chunks.append(f"=== {job.name} (job {job.id}) ===\n{text}")
        if not chunks:
            raise RunLogUnavailableError(
                f"gh: no retrievable logs for any failed job of run {run_id}"
            )
        return "\n\n".join(chunks)

    async def run_failed_annotations(self, repo: str, run_id: str) -> str:
        """Concise failure reasons from each failed job's check-run annotations.

        Crucial when a job's log blob is unavailable: a self-hosted runner that
        lost communication mid-step never uploads its log (the per-job logs API
        404s), but the check-run annotation still records the reason ("The
        self-hosted runner lost communication with the server …"). Read-only;
        returns "" when there are no failed jobs or no annotations."""
        if not repo or not run_id:
            return ""
        failed = await self._failed_jobs(repo, run_id)
        lines: list[str] = []
        for job in failed:
            for level, msg in await self._job_annotations(repo, job.id):
                lines.append(f"[{level}] {job.name}: {msg}")
        return "\n".join(lines)

    async def _job_annotations(self, repo: str, job_id: str) -> list[tuple[str, str]]:
        """`(level, message)` from a job's check-run annotations. The check-run id
        equals the Actions job databaseId. Tolerant — returns [] on any failure."""
        result = await self._run("api", f"/repos/{repo}/check-runs/{job_id}/annotations")
        if result.returncode != 0:
            return []
        try:
            data: object = json.loads(_safe_decode(result.stdout))
        except (ValueError, TypeError):
            return []
        out: list[tuple[str, str]] = []
        if isinstance(data, list):
            for ann in cast("list[Any]", data):
                if not isinstance(ann, dict):
                    continue
                ann_d = cast("dict[str, Any]", ann)
                msg = str(ann_d.get("message") or "").strip()
                if msg:
                    out.append((str(ann_d.get("annotation_level") or "note"), msg))
        return out

    async def failed_jobs(self, repo: str, run_id: str) -> list[FailedJob]:
        """Public: failed/cancelled/timed-out jobs of a run (id, name, runner,
        window). Used by the handler to resolve a Loki host (runner fallback) +
        time window. Returns [] on any failure."""
        return await self._failed_jobs(repo, run_id)

    async def run_meta(self, repo: str, run_id: str) -> WorkflowRunMeta:
        """Identity of one run (workflow_id, head_sha, branch) for cross-run
        comparison. `GET /repos/{repo}/actions/runs/{run_id}` (feature 003 P1)."""
        payload = await self._api("GET", f"/repos/{repo}/actions/runs/{run_id}")
        if not isinstance(payload, dict):
            raise PermanentError("gh run_meta returned non-object")
        data = cast("dict[str, Any]", payload)
        wf = data.get("workflow_id")
        attempt = data.get("run_attempt")
        return WorkflowRunMeta(
            run_id=run_id,
            workflow_id=str(wf) if wf is not None else None,
            head_sha=_opt_str(data.get("head_sha")),
            head_branch=_opt_str(data.get("head_branch")),
            event=_opt_str(data.get("event")),
            run_attempt=attempt if isinstance(attempt, int) else None,
        )

    async def list_workflow_runs(
        self,
        repo: str,
        *,
        workflow_id: str,
        per_page: int = 30,
        branch: str | None = None,
    ) -> list[RunSummary]:
        """Recent COMPLETED runs of a workflow, newest first (feature 003 P1).
        `GET /repos/{repo}/actions/workflows/{workflow_id}/runs?status=completed`.
        `branch` is omitted by default so the listing spans OTHER PRs (the whole
        point of the comparison), not just this PR's reruns."""
        extra = ["-f", f"per_page={per_page}", "-f", "status=completed"]
        if branch:
            extra += ["-f", f"branch={branch}"]
        payload = await self._api(
            "GET",
            f"/repos/{repo}/actions/workflows/{workflow_id}/runs",
            extra=tuple(extra),
        )
        runs_raw = payload.get("workflow_runs") if isinstance(payload, dict) else None
        out: list[RunSummary] = []
        if isinstance(runs_raw, list):
            for raw in cast("list[Any]", runs_raw):
                if not isinstance(raw, dict):
                    continue
                rd = cast("dict[str, Any]", raw)
                rid = rd.get("id")
                if rid is None:
                    continue
                out.append(
                    RunSummary(
                        id=str(rid),
                        head_sha=_opt_str(rd.get("head_sha")),
                        head_branch=_opt_str(rd.get("head_branch")),
                        status=_opt_str(rd.get("status")),
                        conclusion=_opt_str(rd.get("conclusion")),
                        event=_opt_str(rd.get("event")),
                        created_at=_opt_str(rd.get("created_at")),
                    )
                )
        return out

    async def _failed_jobs(self, repo: str, run_id: str) -> list[FailedJob]:
        """Failed/cancelled/timed-out jobs of the run, with runner + window."""
        payload = await self._api(
            "GET",
            f"/repos/{repo}/actions/runs/{run_id}/jobs",
            extra=("-f", "per_page=100", "-f", "filter=latest"),
        )
        jobs_raw = payload.get("jobs") if isinstance(payload, dict) else None
        out: list[FailedJob] = []
        if isinstance(jobs_raw, list):
            for job in cast("list[Any]", jobs_raw):
                if not isinstance(job, dict):
                    continue
                job_d = cast("dict[str, Any]", job)
                if job_d.get("conclusion") in ("failure", "cancelled", "timed_out"):
                    jid = job_d.get("id")
                    if jid is not None:
                        out.append(
                            FailedJob(
                                id=str(jid),
                                name=str(job_d.get("name") or jid),
                                runner_name=_opt_str(job_d.get("runner_name")),
                                started_at=_opt_str(job_d.get("started_at")),
                                completed_at=_opt_str(job_d.get("completed_at")),
                            )
                        )
        return out

    async def _job_logs(self, repo: str, job_id: str) -> str | None:
        """Raw logs for one job via `gh api /repos/.../actions/jobs/<id>/logs`.
        Returns None when the blob is gone (404 / BlobNotFound) so the caller can
        skip just that job; raises (auth / rate / 5xx) for real failures."""
        result = await self._run("api", f"/repos/{repo}/actions/jobs/{job_id}/logs")
        if result.returncode == 0:
            return _safe_decode(result.stdout)
        blob = (_safe_decode(result.stderr) + _safe_decode(result.stdout)).lower()
        if any(p in blob for p in _RUN_LOG_UNAVAILABLE_PHRASES) or "404" in blob:
            return None
        self._raise_run_log_error(repo, job_id, result)
        return None  # unreachable — _raise_run_log_error always raises

    async def pr_get(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch one PR's metadata via `GET /repos/{repo}/pulls/{n}`."""
        payload = await self._api("GET", f"/repos/{repo}/pulls/{pr_number}")
        if not isinstance(payload, dict):
            raise PermanentError("gh pr_get returned non-object")
        return payload

    async def pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Fetch the changed-files list via `GET /repos/{repo}/pulls/{n}/files`."""
        payload = await self._api(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/files",
            extra=("-f", "per_page=100"),
            paginate=True,
        )
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def post_review(
        self,
        repo: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
        event: ReviewEvent = "COMMENT",
        login: str | None = None,
    ) -> dict[str, Any]:
        """Post one review object. `event` defaults to "COMMENT".

        Pass `event="APPROVE"` to submit a GitHub APPROVE review (counts
        toward branch protection); the handler picks this when finding
        count is zero. Self-approval is rejected by GitHub when the bot's
        login equals the PR author — the handler's `skipped_self_authored`
        gate already prevents the request from reaching us in that case.

        On HTTP 5xx (server accepted the POST then died on the response leg),
        if `login` is provided, probe the reviews list for a matching
        `(commit_id, login)` review and return it as if the POST had
        succeeded — the GitHub server already created the row, so retrying
        would post a duplicate. If the probe finds nothing or itself fails,
        the original `TransientError` propagates so the dispatcher retries.
        """
        request: dict[str, Any] = {
            "commit_id": commit_id,
            "event": event,
            "body": body,
            "comments": comments,
        }
        try:
            payload = await self._api(
                "POST",
                f"/repos/{repo}/pulls/{pr_number}/reviews",
                stdin_json=request,
            )
        except TransientError as exc:
            if login is not None and _is_http_5xx(str(exc)):
                existing = await self._discover_existing_review(
                    repo, pr_number, commit_id=commit_id, login=login
                )
                if existing is not None:
                    return existing
            raise
        if not isinstance(payload, dict):
            raise PermanentError("gh post review returned non-object")
        return payload

    async def list_reviews_at(
        self,
        repo: str,
        pr_number: int,
        *,
        commit_id: str,
        login: str,
    ) -> list[dict[str, Any]]:
        """Return submitted reviews on `pr_number` matching `(commit_id, login)`.

        Pending reviews (`submitted_at == null`) are excluded — those don't
        count as posted. `commit_id` filtering is client-side because the
        endpoint doesn't accept a SHA filter.
        """
        payload = await self._api(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            extra=("-f", "per_page=100"),
            paginate=True,
        )
        if not isinstance(payload, list):
            return []
        out: list[dict[str, Any]] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            if raw.get("commit_id") != commit_id:
                continue
            if raw.get("submitted_at") in (None, ""):
                continue
            user = raw.get("user")
            if not isinstance(user, dict) or user.get("login") != login:
                continue
            out.append(cast("dict[str, Any]", raw))
        return out

    async def list_prior_reviews_with_comments(  # noqa: PLR0912 — fan-out on GH payload shape
        self,
        repo: str,
        pr_number: int,
        *,
        login: str,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Return the most recent <= `limit` submitted reviews on this PR by `login`,
        each with its inline comments attached under `inline_comments`.

        Used to give the persona context for re-review buckets
        (Resolved / Still open / New). On any fetch error returns `[]` —
        prior context is a nice-to-have, never a triage blocker.
        """
        try:
            reviews_payload = await self._api(
                "GET",
                f"/repos/{repo}/pulls/{pr_number}/reviews",
                extra=("-f", "per_page=100"),
                paginate=True,
            )
        except Exception:
            return []
        if not isinstance(reviews_payload, list):
            return []

        reviews: list[dict[str, Any]] = []
        for raw in reviews_payload:
            if not isinstance(raw, dict):
                continue
            user = raw.get("user")
            if not isinstance(user, dict) or user.get("login") != login:
                continue
            submitted = raw.get("submitted_at")
            if submitted in (None, ""):
                continue
            reviews.append(cast("dict[str, Any]", raw))

        reviews.sort(key=lambda r: str(r.get("submitted_at", "")), reverse=True)
        recent = reviews[:limit]
        if not recent:
            return []

        # Pull all PR-level review comments once; filter client-side.
        try:
            comments_payload = await self._api(
                "GET",
                f"/repos/{repo}/pulls/{pr_number}/comments",
                extra=("-f", "per_page=100"),
                paginate=True,
            )
        except Exception:
            comments_payload = []
        comments_by_review: dict[int, list[dict[str, Any]]] = {}
        if isinstance(comments_payload, list):
            for raw in comments_payload:
                if not isinstance(raw, dict):
                    continue
                rid = raw.get("pull_request_review_id")
                if not isinstance(rid, int):
                    continue
                comments_by_review.setdefault(rid, []).append(cast("dict[str, Any]", raw))

        for r in recent:
            rid = r.get("id")
            r["inline_comments"] = comments_by_review.get(rid, []) if isinstance(rid, int) else []
        return recent

    async def _discover_existing_review(
        self,
        repo: str,
        pr_number: int,
        *,
        commit_id: str,
        login: str,
    ) -> dict[str, Any] | None:
        """Best-effort dedup probe. On any failure return None — the original
        TransientError will propagate and the dispatcher will retry."""
        try:
            matches = await self.list_reviews_at(repo, pr_number, commit_id=commit_id, login=login)
        except Exception:
            # Best-effort dedup: any failure means the original TransientError
            # propagates and the dispatcher retries. Documented in post_review.
            return None
        if not matches:
            return None
        # Take the most recently submitted matching review.
        return max(matches, key=lambda r: str(r.get("submitted_at", "")))

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _api(
        self,
        method: str,
        path: str,
        *,
        extra: tuple[str, ...] = (),
        paginate: bool = False,
        stdin_json: dict[str, Any] | None = None,
    ) -> Any:
        args: list[str] = ["api", "-X", method]
        if paginate:
            args.append("--paginate")
        if stdin_json is not None:
            args.extend(["--input", "-"])
        args.append(path)
        args.extend(extra)

        stdin_bytes = json.dumps(stdin_json).encode("utf-8") if stdin_json is not None else None
        result = await self._run(*args, stdin=stdin_bytes)
        if result.returncode != 0:
            self._raise_error(method, path, result)
        text = _safe_decode(result.stdout).strip()
        if not text:
            return {}
        if paginate and not text.startswith("["):
            # `gh api --paginate` concatenates JSON arrays as `[...]\n[...]`.
            # When the endpoint already returns an object with `items` (e.g.
            # /search/issues), gh emits multiple objects; merge them.
            return _merge_paginated_objects(text)
        if paginate and text.startswith("["):
            return _merge_paginated_arrays(text)
        return json.loads(text)

    async def _run(self, *args: str, stdin: bytes | None = None) -> _GhResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise PermanentError(f"gh CLI not found on PATH: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=self._timeout)
        except TimeoutError as exc:
            with _suppress():
                proc.kill()
            raise TransientError(f"gh {args[0]} timed out after {self._timeout}s") from exc
        return _GhResult(returncode=proc.returncode or 0, stdout=stdout, stderr=stderr)

    def _raise_run_log_error(self, repo: str, run_id: str, result: _GhResult) -> None:
        """Dedicated classifier for `gh run view --log-failed` (NOT gh-api shaped).

        Order: auth → HTTP 401/403-rate/5xx → log-unavailable phrases → transient.
        A gone/expired log (no HTTP code) → RunLogUnavailableError (skip, not retry).
        """
        stderr = _safe_decode(result.stderr)
        lower = stderr.lower()
        where = f"gh run view {run_id} --repo {repo} --log-failed"

        if any(p in lower for p in _AUTH_PHRASES):
            raise AuthError(f"{where}: auth failure: {stderr.strip()}")

        match = _HTTP_CODE_RE.search(stderr)
        code = int(match.group(1)) if match else None
        if code == 401:
            raise AuthError(f"{where}: HTTP 401: {stderr.strip()}")
        if code == 403 and any(p in lower for p in _RATE_LIMIT_PHRASES):
            raise RateLimitError(f"{where}: rate-limited: {stderr.strip()}")
        if code is not None and 500 <= code < 600:
            raise TransientError(f"{where}: HTTP {code}: {stderr.strip()}")

        if any(p in lower for p in _RUN_LOG_UNAVAILABLE_PHRASES):
            raise RunLogUnavailableError(f"{where}: log unavailable: {stderr.strip()}")

        # Genuinely transient (network / timeout / unknown non-zero exit).
        raise TransientError(f"{where}: exit {result.returncode}: {stderr.strip()}")

    def _raise_error(self, method: str, path: str, result: _GhResult) -> None:
        stderr = _safe_decode(result.stderr)
        lower = stderr.lower()

        if any(p in lower for p in _AUTH_PHRASES):
            raise AuthError(f"gh {method} {path}: auth failure: {stderr.strip()}")

        match = _HTTP_CODE_RE.search(stderr)
        code = int(match.group(1)) if match else None

        if code == 401:
            raise AuthError(f"gh {method} {path}: HTTP 401: {stderr.strip()}")
        if code == 403:
            if any(p in lower for p in _RATE_LIMIT_PHRASES):
                raise RateLimitError(f"gh {method} {path}: rate-limited: {stderr.strip()}")
            raise PermanentError(f"gh {method} {path}: HTTP 403: {stderr.strip()}")
        if code == 404 and method == "GET":
            raise PermanentError(f"gh {method} {path}: HTTP 404: PR not found or no access")
        if code == 422 and method == "POST":
            raise PermanentError(f"gh {method} {path}: HTTP 422: {stderr.strip()}")
        if code is not None and 500 <= code < 600:
            raise TransientError(f"gh {method} {path}: HTTP {code}: {stderr.strip()}")

        # No identifiable HTTP code — treat as transient if exit code is non-zero.
        raise TransientError(f"gh {method} {path}: exit {result.returncode}: {stderr.strip()}")


# ── Module helpers ────────────────────────────────────────────────────────


def _safe_decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("utf-8", errors="replace")


def _merge_paginated_arrays(text: str) -> list[Any]:
    """`gh --paginate` on array endpoints emits `[..]\\n[..]\\n...`."""
    out: list[Any] = []
    for loaded in _iter_json_documents(text):
        if isinstance(loaded, list):
            out.extend(loaded)  # type: ignore[arg-type]
        else:
            out.append(loaded)
    return out


def _merge_paginated_objects(text: str) -> dict[str, Any]:
    """`gh --paginate` on `/search/issues` emits one JSON object per page.

    Concatenate `items` lists; preserve `total_count` from the first page.
    """
    merged: dict[str, Any] = {}
    items: list[Any] = []
    for loaded in _iter_json_documents(text):
        if not isinstance(loaded, dict):
            continue
        if not merged:
            merged = {str(k): v for k, v in loaded.items() if k != "items"}  # type: ignore[misc]
        page_items: Any = loaded.get("items", [])
        if isinstance(page_items, list):
            items.extend(page_items)  # type: ignore[arg-type]
    merged["items"] = items
    return merged


def _iter_json_documents(text: str) -> list[Any]:
    """Parse a `gh --paginate` blob into a list of JSON documents.

    `gh --paginate` concatenates one JSON object per page back-to-back with
    no separator. `JSONDecoder.raw_decode` peels them off one at a time —
    no hand-rolled brace-depth state machine, and the JSON parser handles
    quoting/escapes correctly by construction.
    """
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    i = 0
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        obj, end = decoder.raw_decode(text, i)
        documents.append(obj)
        i = end
    return documents


class _suppress:
    """tiny contextlib.suppress(BaseException) without the import."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a: object) -> bool:
        return True


__all__ = ["FailedJob", "GhCli", "RunSummary", "WorkflowRunMeta"]
