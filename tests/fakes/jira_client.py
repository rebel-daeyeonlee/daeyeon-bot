"""In-memory `JiraClient` substitute for unit + integration tests.

Mirrors the public method surface of `infra.jira_client.JiraClient` so
the handler / trigger can be wired against it via the same constructor
argument. Backed by a dict of canned issues and a posted-comments list
for assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from daeyeon_bot.infra.jira_client import (
    FieldDiscovery,
    IssueDetail,
    IssueSummary,
    JiraIdentity,
    PostedComment,
    SearchPage,
)


@dataclass(slots=True)
class _FakeIssue:
    key: str
    summary: str
    created_iso: str
    project: str
    issuetype_name: str
    assignee_account_id: str | None
    parent_key: str | None
    status_name: str
    description_text: str
    custom_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _FakeComment:
    key: str
    body_wiki: str
    posted_at: datetime
    comment_id: str


class FakeJiraClient:
    """Test double for `JiraClient`. All methods are sync-ish async."""

    def __init__(
        self,
        *,
        identity: JiraIdentity | None = None,
        field_discovery: FieldDiscovery | None = None,
    ) -> None:
        self._identity = identity or JiraIdentity(
            account_id="557058:fake",
            email_address="daeyeon.lee@rebellions.ai",
            display_name="daeyeon",
        )
        self._field_discovery = field_discovery or FieldDiscovery(
            branch_field_id="customfield_10042",
            commit_field_id="customfield_10043",
            team_field_id="customfield_10050",
            issuetype_name="Bug",
        )
        self._issues: dict[str, _FakeIssue] = {}
        self._posted: list[_FakeComment] = []
        self._comment_seq = 10_000

    # ── Test seeding API ────────────────────────────────────────────────────

    def add_issue(
        self,
        *,
        key: str,
        summary: str,
        created_iso: str | None = None,
        project: str | None = None,
        issuetype_name: str = "Bug",
        assignee_account_id: str | None = None,
        parent_key: str | None = None,
        status_name: str = "Open",
        description_text: str = "",
        custom_fields: dict[str, Any] | None = None,
    ) -> None:
        proj = project or key.split("-", 1)[0]
        self._issues[key] = _FakeIssue(
            key=key,
            summary=summary,
            created_iso=created_iso or "2026-05-13T07:00:00.000+0000",
            project=proj,
            issuetype_name=issuetype_name,
            assignee_account_id=assignee_account_id,
            parent_key=parent_key,
            status_name=status_name,
            description_text=description_text,
            custom_fields=dict(custom_fields or {}),
        )

    def remove_issue(self, key: str) -> None:
        self._issues.pop(key, None)

    def update_assignee(self, key: str, account_id: str | None) -> None:
        if key in self._issues:
            issue = self._issues[key]
            self._issues[key] = _FakeIssue(
                **{**issue.__dict__, "assignee_account_id": account_id}  # type: ignore[arg-type]
            )

    def posted_comments(self) -> list[_FakeComment]:
        return list(self._posted)

    # ── JiraClient API ──────────────────────────────────────────────────────

    async def myself(self) -> JiraIdentity:
        return self._identity

    async def discover_fields(
        self,
        *,
        project_keys: list[str],
        issuetype_candidates: tuple[str, ...] = ("TC Failure", "Bug"),
    ) -> FieldDiscovery:
        del project_keys, issuetype_candidates  # canned
        return self._field_discovery

    async def search_jql(
        self,
        *,
        jql: str,
        fields: list[str],
        start_at: int = 0,
        max_results: int = 50,
    ) -> SearchPage:
        del fields  # FakeJira returns everything
        matching = [iss for iss in self._issues.values() if _jql_matches(iss, jql)]
        page = matching[start_at : start_at + max_results]
        summaries = tuple(
            IssueSummary(
                key=iss.key,
                summary=iss.summary,
                created_iso=iss.created_iso,
                assignee_account_id=iss.assignee_account_id,
                parent_key=iss.parent_key,
                status_name=iss.status_name,
                raw_fields={
                    "summary": iss.summary,
                    "created": iss.created_iso,
                    "assignee": (
                        {"accountId": iss.assignee_account_id} if iss.assignee_account_id else None
                    ),
                    "parent": ({"key": iss.parent_key} if iss.parent_key else None),
                    "status": {"name": iss.status_name},
                    **iss.custom_fields,
                },
            )
            for iss in page
        )
        return SearchPage(
            start_at=start_at,
            max_results=max_results,
            total=len(matching),
            issues=summaries,
        )

    async def issue_get(
        self,
        key: str,
        *,
        expand: list[str] | None = None,
    ) -> IssueDetail:
        del expand
        iss = self._issues.get(key)
        if iss is None:
            from daeyeon_bot.core.errors import PermanentError

            raise PermanentError(f"jira GET /issue/{key}: HTTP 404 not found")
        return IssueDetail(
            key=iss.key,
            summary=iss.summary,
            description_text=iss.description_text,
            reporter_account_id=None,
            assignee_account_id=iss.assignee_account_id,
            parent_key=iss.parent_key,
            status_name=iss.status_name,
            raw_fields={
                "summary": iss.summary,
                "created": iss.created_iso,
                "parent": ({"key": iss.parent_key} if iss.parent_key else None),
                **iss.custom_fields,
            },
        )

    async def post_comment(self, key: str, *, body_wiki: str) -> PostedComment:
        if not isinstance(body_wiki, str):  # type: ignore[unreachable]
            raise TypeError("FakeJira post_comment requires str body")
        self._comment_seq += 1
        posted = _FakeComment(
            key=key,
            body_wiki=body_wiki,
            posted_at=datetime.now(tz=UTC),
            comment_id=str(self._comment_seq),
        )
        self._posted.append(posted)
        return PostedComment(
            comment_id=posted.comment_id,
            posted_at=posted.posted_at,
            self_url=f"https://fake.atlassian.net/issue/{key}/comment/{posted.comment_id}",
        )


def _jql_matches(iss: _FakeIssue, jql: str) -> bool:
    """Minimal JQL parser — enough to drive the handler tests.

    Supported clauses (AND-joined, case-insensitive substring on `summary`):
      - `project = "X"` / `project IN ("X","Y")`
      - `assignee = currentUser()`
      - `"Team" = "X"`
      - `summary ~ "X"`
      - `status != Closed`
      - `(... OR ...)` — at most one OR group on assignee/team.
    """
    text = jql.lower()
    # Closed-status filter.
    if "status != closed" in text and iss.status_name.lower() == "closed":
        return False
    # Summary fuzzy.
    if 'summary ~ "regression-test"' in text and "regression-test" not in iss.summary.lower():
        return False
    # Project filter.
    proj_lower = iss.project.lower()
    if 'project = "' in text:
        wanted = text.split('project = "', 1)[1].split('"', 1)[0]
        if wanted != proj_lower:
            return False
    elif "project in (" in text:
        group = text.split("project in (", 1)[1].split(")", 1)[0]
        wanted_projs = [p.strip().strip('"').lower() for p in group.split(",")]
        if proj_lower not in wanted_projs:
            return False
    # Assignee / Team OR group.
    or_group_match = False
    has_or_group = "assignee = currentuser()" in text or '"team" = "' in text
    if has_or_group:
        if "assignee = currentuser()" in text and iss.assignee_account_id == "557058:fake":
            or_group_match = True
        if '"team" = "' in text:
            team_val = text.split('"team" = "', 1)[1].split('"', 1)[0]
            if iss.custom_fields.get("team_name", "").lower() == team_val:
                or_group_match = True
        if not or_group_match:
            return False
    return True


__all__ = ["FakeJiraClient"]
