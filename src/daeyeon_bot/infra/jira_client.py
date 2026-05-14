"""Async wrapper around the Jira REST API for the daeyeon-bot daemon.

Auth: HTTP basic `(JIRA_USER, JIRA_API_TOKEN)` — matches the convention
already used by `ssw-bundle/inv/test_report/jira_client.py` so the
operator manages one token across both tools.

REST version mix:
  - v3 for search / issue-get / createmeta — better field metadata
  - v2 for `POST .../comment` — accepts plain wiki-markup `body`
  Both share one `httpx.AsyncClient` instance with the same basic auth.

The 6 endpoints exposed here are the entire Jira surface used by the
daemon. See `specs/002-jira-triage-bot/contracts/jira-rest-api-surface.md`
for the canonical list and the explicit ban-list.

Error mapping (per the contract):
    HTTP 401 / 403           → AuthError       (daemon halts, exit 78)
    HTTP 429                 → RateLimitError  (Retry with Retry-After backoff)
    HTTP 400 on POST comment → PermanentError  (DeadLetter; wiki-markup bug)
    HTTP 404 on GET issue    → PermanentError  (issue not found / no access)
    HTTP 5xx / timeout       → TransientError  (Retry default backoff)

No retries inside the wrapper; the dispatcher / trigger handles them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import httpx

from daeyeon_bot.core.errors import (
    AuthError,
    PermanentError,
    RateLimitError,
    TransientError,
)

_REST_V3 = "/rest/api/3"
_REST_V2 = "/rest/api/2"


@dataclass(frozen=True, slots=True)
class JiraIdentity:
    """Result of `GET /rest/api/3/myself` — daemon-lifetime cached."""

    account_id: str
    email_address: str
    display_name: str


@dataclass(frozen=True, slots=True)
class FieldDiscovery:
    """Result of `GET /rest/api/3/issue/createmeta` — daemon-lifetime cached."""

    branch_field_id: str  # e.g. "customfield_10042"
    commit_field_id: str  # e.g. "customfield_10043"
    team_field_id: str  # e.g. "customfield_10050" — empty if Team field not in schema
    issuetype_name: str  # e.g. "Bug" or "TC Failure"


@dataclass(frozen=True, slots=True)
class IssueSummary:
    """One row of a JQL search result."""

    key: str
    summary: str
    created_iso: str
    assignee_account_id: str | None
    parent_key: str | None
    status_name: str
    raw_fields: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of `GET /rest/api/3/search`."""

    start_at: int
    max_results: int
    total: int
    issues: tuple[IssueSummary, ...]


@dataclass(frozen=True, slots=True)
class IssueDetail:
    """One full issue from `GET /rest/api/3/issue/{key}`."""

    key: str
    summary: str
    description_text: str  # extracted from ADF or plain string
    reporter_account_id: str | None
    assignee_account_id: str | None
    parent_key: str | None
    status_name: str
    raw_fields: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PostedComment:
    """Result of `POST /rest/api/2/issue/{key}/comment`."""

    comment_id: str
    posted_at: datetime
    self_url: str


_COMMENT_BODY_MAX = 32_000  # Atlassian comment cap


class JiraClient:
    """Thin httpx wrapper. One instance per daemon."""

    def __init__(
        self,
        *,
        base_url: str,
        user: str,
        token: str,
        timeout_s: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        # Normalize trailing slash so endpoint concatenation is deterministic.
        self._base_url = base_url.rstrip("/")
        self._auth = httpx.BasicAuth(user, token)
        self._timeout = timeout_s
        self._http = http  # caller-injected (for testing); else built per-request

    # ── Public API ──────────────────────────────────────────────────────────

    async def myself(self) -> JiraIdentity:
        data = await self._get(f"{_REST_V3}/myself")
        return JiraIdentity(
            account_id=str(data.get("accountId", "")),
            email_address=str(data.get("emailAddress", "")),
            display_name=str(data.get("displayName", "")),
        )

    async def discover_fields(
        self,
        *,
        project_keys: list[str],
        issuetype_candidates: tuple[str, ...] = ("TC Failure", "Bug"),
    ) -> FieldDiscovery:
        """Probe `createmeta` once to learn the Branch/Commit/Team custom-field IDs.

        Match policy:
          - issuetype: first candidate that exists in the project's issuetypes.
          - branch_field_id: first field whose `name` == "Branch" (case-insensitive).
          - commit_field_id: same with "Commit".
          - team_field_id:   same with "Team"; empty string if not present.

        Raises ConfigError-flavored PermanentError if the project schema
        lacks both Branch and Commit fields — feature can't operate.
        """
        params = {
            "projectKeys": ",".join(project_keys),
            "expand": "projects.issuetypes.fields",
        }
        data = await self._get(f"{_REST_V3}/issue/createmeta", params=params)
        projects = cast("list[dict[str, Any]]", data.get("projects", []))
        if not projects:
            raise PermanentError(f"jira discover_fields: no project metadata for {project_keys}")
        project_block = projects[0]
        issuetypes = cast("list[dict[str, Any]]", project_block.get("issuetypes", []))

        chosen_name = ""
        chosen_fields: dict[str, Any] = {}
        candidate_lookup = {c.lower(): c for c in issuetype_candidates}
        for itype in issuetypes:
            name = str(itype.get("name", ""))
            if name.lower() in candidate_lookup:
                chosen_name = name
                chosen_fields = cast("dict[str, Any]", itype.get("fields", {}))
                break
        if not chosen_name and issuetypes:
            # Fall back to first available issuetype + its fields.
            chosen_name = str(issuetypes[0].get("name", ""))
            chosen_fields = cast("dict[str, Any]", issuetypes[0].get("fields", {}))

        branch_id = _field_id_by_name(chosen_fields, "Branch")
        commit_id = _field_id_by_name(chosen_fields, "Commit")
        team_id = _field_id_by_name(chosen_fields, "Team")
        if not branch_id or not commit_id:
            raise PermanentError(
                "jira discover_fields: project schema missing Branch and/or Commit"
                f" custom field (branch={branch_id!r} commit={commit_id!r});"
                " set [handlers.jira_triage].branch_field_id and .commit_field_id"
                " explicitly to override"
            )
        return FieldDiscovery(
            branch_field_id=branch_id,
            commit_field_id=commit_id,
            team_field_id=team_id,
            issuetype_name=chosen_name,
        )

    async def search_jql(
        self,
        *,
        jql: str,
        fields: list[str],
        start_at: int = 0,
        max_results: int = 50,
    ) -> SearchPage:
        params = {
            "jql": jql,
            "fields": ",".join(fields),
            "startAt": str(start_at),
            "maxResults": str(max_results),
        }
        data = await self._get(f"{_REST_V3}/search", params=params)
        issues_raw = cast("list[dict[str, Any]]", data.get("issues", []))
        issues: list[IssueSummary] = []
        for raw in issues_raw:
            f = cast("dict[str, Any]", raw.get("fields", {}))
            assignee_block = cast("dict[str, Any] | None", f.get("assignee"))
            parent_block = cast("dict[str, Any] | None", f.get("parent"))
            status_block = cast("dict[str, Any] | None", f.get("status"))
            issues.append(
                IssueSummary(
                    key=str(raw.get("key", "")),
                    summary=str(f.get("summary", "")),
                    created_iso=str(f.get("created", "")),
                    assignee_account_id=(
                        str(assignee_block["accountId"]) if assignee_block else None
                    ),
                    parent_key=str(parent_block["key"]) if parent_block else None,
                    status_name=str(status_block["name"]) if status_block else "",
                    raw_fields=f,
                )
            )
        return SearchPage(
            start_at=int(data.get("startAt", start_at)),
            max_results=int(data.get("maxResults", max_results)),
            total=int(data.get("total", len(issues))),
            issues=tuple(issues),
        )

    async def issue_get(
        self,
        key: str,
        *,
        expand: list[str] | None = None,
    ) -> IssueDetail:
        params: dict[str, str] = {}
        if expand:
            params["expand"] = ",".join(expand)
        data = await self._get(f"{_REST_V3}/issue/{key}", params=params or None)
        fields = cast("dict[str, Any]", data.get("fields", {}))
        assignee_block = cast("dict[str, Any] | None", fields.get("assignee"))
        reporter_block = cast("dict[str, Any] | None", fields.get("reporter"))
        parent_block = cast("dict[str, Any] | None", fields.get("parent"))
        status_block = cast("dict[str, Any] | None", fields.get("status"))
        description_text = _adf_to_text(fields.get("description"))
        return IssueDetail(
            key=str(data.get("key", key)),
            summary=str(fields.get("summary", "")),
            description_text=description_text,
            reporter_account_id=(str(reporter_block["accountId"]) if reporter_block else None),
            assignee_account_id=(str(assignee_block["accountId"]) if assignee_block else None),
            parent_key=str(parent_block["key"]) if parent_block else None,
            status_name=str(status_block["name"]) if status_block else "",
            raw_fields=fields,
        )

    async def post_comment(
        self,
        key: str,
        *,
        body_wiki: str,
    ) -> PostedComment:
        """Post a wiki-markup comment via REST v2. Refuses non-string body."""
        if not isinstance(body_wiki, str):  # type: ignore[unreachable]
            raise TypeError(f"post_comment body must be str (wiki markup), got {type(body_wiki)}")
        if not body_wiki.strip():
            raise PermanentError("post_comment: empty body")
        if len(body_wiki) > _COMMENT_BODY_MAX:
            raise PermanentError(
                f"post_comment: body {len(body_wiki)} chars > Atlassian cap {_COMMENT_BODY_MAX}"
            )
        data = await self._post(
            f"{_REST_V2}/issue/{key}/comment",
            json_body={"body": body_wiki},
        )
        return PostedComment(
            comment_id=str(data.get("id", "")),
            posted_at=_parse_jira_datetime(str(data.get("created", ""))),
            self_url=str(data.get("self", "")),
        )

    # ── HTTP plumbing ───────────────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _post(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request("POST", path, json_body=json_body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            if self._http is not None:
                response = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    auth=self._auth,
                    timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=headers,
                        auth=self._auth,
                        timeout=self._timeout,
                    )
        except httpx.TimeoutException as exc:
            raise TransientError(f"jira {method} {path}: timeout ({exc})") from exc
        except httpx.RequestError as exc:
            raise TransientError(f"jira {method} {path}: network error ({exc})") from exc
        return _classify_response(method, path, response)

    def replace_http(self, http: httpx.AsyncClient | None) -> None:
        """Test seam: swap in a mock-transport client."""
        self._http = http


# ── Response classification ──────────────────────────────────────────────────


def _classify_response(method: str, path: str, response: httpx.Response) -> dict[str, Any]:
    status = response.status_code
    if 200 <= status < 300:
        if status == 204 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise TransientError(f"jira {method} {path}: non-JSON body ({exc})") from exc
        if not isinstance(data, dict):
            return {"items": data}
        return cast("dict[str, Any]", data)

    body_excerpt = response.text[:200] if response.text else ""
    if status in (401, 403):
        raise AuthError(f"jira {method} {path}: HTTP {status} {body_excerpt}")
    if status == 429:
        retry_after_raw = response.headers.get("Retry-After", "0")
        try:
            retry_after = float(retry_after_raw)
        except ValueError:
            retry_after = 0.0
        raise RateLimitError(
            f"jira {method} {path}: HTTP 429 retry_after={retry_after}s {body_excerpt}"
        )
    if status == 404 and method == "GET":
        raise PermanentError(f"jira {method} {path}: HTTP 404 not found")
    if status == 400 and method == "POST":
        raise PermanentError(f"jira {method} {path}: HTTP 400 {body_excerpt}")
    if 500 <= status < 600:
        raise TransientError(f"jira {method} {path}: HTTP {status} {body_excerpt}")
    raise PermanentError(f"jira {method} {path}: HTTP {status} {body_excerpt}")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _field_id_by_name(fields: dict[str, Any], target_name: str) -> str:
    """Return the customfield id whose `name` matches `target_name` (CI), or ''."""
    target = target_name.lower()
    for field_id, field_def in fields.items():
        if not isinstance(field_def, dict):
            continue
        name_obj = cast("dict[str, Any]", field_def).get("name")
        if isinstance(name_obj, str) and name_obj.lower() == target:
            return str(field_id)
    return ""


def _adf_to_text(adf: object) -> str:
    """Best-effort ADF→plain-text. Returns input string if not ADF."""
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""
    adf_typed = cast("dict[str, Any]", adf)
    pieces: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            node_typed = cast("dict[str, Any]", node)
            if node_typed.get("type") == "text" and isinstance(node_typed.get("text"), str):
                pieces.append(str(node_typed["text"]))
            content = node_typed.get("content")
            if isinstance(content, list):
                for child in cast("list[Any]", content):
                    _walk(child)
            # Paragraph / hardBreak boundaries add newlines.
            if node_typed.get("type") in ("paragraph", "hardBreak"):
                pieces.append("\n")
        elif isinstance(node, list):
            for child in cast("list[Any]", node):
                _walk(child)

    _walk(adf_typed)
    return "".join(pieces).strip()


def _parse_jira_datetime(raw: str) -> datetime:
    """Jira returns `2026-05-13T07:15:02.123+0000`. fromisoformat needs `+00:00`."""
    if not raw:
        # Fallback — return UTC now-ish? We choose to fail loudly here so the
        # caller doesn't get a silent zero-time stamp.
        raise PermanentError("jira response missing `created` timestamp")
    fixed = raw
    # Convert trailing `+0000` → `+00:00` for fromisoformat.
    if len(fixed) >= 5 and (fixed[-5] in "+-") and fixed[-3] != ":":
        fixed = fixed[:-2] + ":" + fixed[-2:]
    return datetime.fromisoformat(fixed)


__all__ = [
    "FieldDiscovery",
    "IssueDetail",
    "IssueSummary",
    "JiraClient",
    "JiraIdentity",
    "PostedComment",
    "SearchPage",
]
