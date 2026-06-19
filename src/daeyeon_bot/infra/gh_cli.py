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
)


def _is_http_5xx(message: str) -> bool:
    """True if `message` mentions an HTTP 5xx code (e.g. a TransientError text)."""
    match = _HTTP_CODE_RE.search(message)
    return match is not None and 500 <= int(match.group(1)) < 600


@dataclass(frozen=True, slots=True)
class _GhResult:
    """Outcome of one `gh` subprocess invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


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


__all__ = ["GhCli"]
