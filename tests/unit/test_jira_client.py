"""JiraClient — T018 tests with httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from daeyeon_bot.core.errors import (
    AuthError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from daeyeon_bot.infra.jira_client import JiraClient


def _client(handler: httpx.MockTransport) -> JiraClient:
    transport_client = httpx.AsyncClient(transport=handler)
    return JiraClient(
        base_url="https://rbln.atlassian.net/",
        user="daeyeon.lee@rebellions.ai",
        token="atok-xyz",
        timeout_s=5.0,
        http=transport_client,
    )


# ── Auth & error mapping ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_401_raises_auth_error() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(401, text="unauth"))
    client = _client(transport)
    with pytest.raises(AuthError, match="HTTP 401"):
        await client.myself()


@pytest.mark.asyncio
async def test_403_also_raises_auth_error() -> None:
    """Atlassian uses 403 for both wrong-creds and missing-permission — both halt."""
    transport = httpx.MockTransport(lambda req: httpx.Response(403))
    client = _client(transport)
    with pytest.raises(AuthError, match="HTTP 403"):
        await client.myself()


@pytest.mark.asyncio
async def test_429_raises_rate_limit_with_retry_after() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(429, headers={"Retry-After": "30"}, text="slow down")
    )
    client = _client(transport)
    with pytest.raises(RateLimitError, match="retry_after=30"):
        await client.myself()


@pytest.mark.asyncio
async def test_404_on_get_raises_permanent() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(404, text="not found"))
    client = _client(transport)
    with pytest.raises(PermanentError, match="HTTP 404"):
        await client.issue_get("SSWCI-9999")


@pytest.mark.asyncio
async def test_5xx_raises_transient() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(503, text="overloaded"))
    client = _client(transport)
    with pytest.raises(TransientError, match="HTTP 503"):
        await client.myself()


@pytest.mark.asyncio
async def test_timeout_raises_transient() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=req)

    transport = httpx.MockTransport(_handler)
    client = _client(transport)
    with pytest.raises(TransientError, match="timeout"):
        await client.myself()


@pytest.mark.asyncio
async def test_400_on_post_comment_raises_permanent() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(400, text='{"errorMessages":["bad markup"]}')
    )
    client = _client(transport)
    with pytest.raises(PermanentError, match="HTTP 400"):
        await client.post_comment("SSWCI-1", body_wiki="h3. test")


# ── Happy paths ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_myself_parses_identity() -> None:
    payload = {
        "accountId": "557058:abcdef",
        "emailAddress": "daeyeon.lee@rebellions.ai",
        "displayName": "daeyeon",
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    client = _client(transport)
    ident = await client.myself()
    assert ident.account_id == "557058:abcdef"
    assert ident.email_address == "daeyeon.lee@rebellions.ai"


@pytest.mark.asyncio
async def test_discover_fields_resolves_branch_and_commit() -> None:
    payload = {
        "projects": [
            {
                "key": "SSWCI",
                "issuetypes": [
                    {
                        "name": "Bug",
                        "fields": {
                            "customfield_10042": {"name": "Branch"},
                            "customfield_10043": {"name": "Commit"},
                            "customfield_10050": {"name": "Team"},
                        },
                    }
                ],
            }
        ]
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    client = _client(transport)
    disc = await client.discover_fields(project_keys=["SSWCI"])
    assert disc.branch_field_id == "customfield_10042"
    assert disc.commit_field_id == "customfield_10043"
    assert disc.team_field_id == "customfield_10050"
    assert disc.issuetype_name == "Bug"


@pytest.mark.asyncio
async def test_discover_fields_raises_when_branch_or_commit_missing() -> None:
    payload = {
        "projects": [
            {
                "key": "SSWCI",
                "issuetypes": [
                    {"name": "Bug", "fields": {"customfield_10042": {"name": "Branch"}}}
                ],
            }
        ]
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    client = _client(transport)
    with pytest.raises(PermanentError, match="missing Branch and/or Commit"):
        await client.discover_fields(project_keys=["SSWCI"])


@pytest.mark.asyncio
async def test_search_jql_parses_issue_summaries() -> None:
    payload = {
        "startAt": 0,
        "maxResults": 50,
        "total": 1,
        "issues": [
            {
                "key": "SSWCI-100",
                "fields": {
                    "summary": "regression-test . ssw-giga-02 . TC-1",
                    "created": "2026-05-13T07:00:00.000+0000",
                    "assignee": {"accountId": "u1"},
                    "parent": {"key": "SSWCI-99"},
                    "status": {"name": "Open"},
                },
            }
        ],
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    client = _client(transport)
    page = await client.search_jql(jql="x", fields=["key"], start_at=0, max_results=50)
    assert page.total == 1
    assert len(page.issues) == 1
    issue = page.issues[0]
    assert issue.key == "SSWCI-100"
    assert issue.assignee_account_id == "u1"
    assert issue.parent_key == "SSWCI-99"
    assert issue.status_name == "Open"


@pytest.mark.asyncio
async def test_issue_get_extracts_adf_description() -> None:
    """ADF→plain-text extraction picks up text nodes verbatim."""
    payload = {
        "key": "SSWCI-1",
        "fields": {
            "summary": "regression-test . h . TC",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Start: 2026-05-13"}],
                    }
                ],
            },
            "status": {"name": "Open"},
        },
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    client = _client(transport)
    detail = await client.issue_get("SSWCI-1")
    assert "Start: 2026-05-13" in detail.description_text


@pytest.mark.asyncio
async def test_issue_get_handles_string_description() -> None:
    """Some Jira instances return wiki-markup string in `description` field."""
    payload = {
        "key": "SSWCI-1",
        "fields": {
            "summary": "x",
            "description": "*Branch*: release/v3.2\n*Commit*: abc123",
            "status": {"name": "Open"},
        },
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    client = _client(transport)
    detail = await client.issue_get("SSWCI-1")
    assert "*Branch*: release/v3.2" in detail.description_text


@pytest.mark.asyncio
async def test_post_comment_returns_posted_struct() -> None:
    payload = {
        "id": "10001",
        "created": "2026-05-13T07:15:02.123+0000",
        "self": "https://rbln.atlassian.net/rest/api/2/issue/SSWCI-1/comment/10001",
    }
    captured: dict[str, object] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json=payload)

    transport = httpx.MockTransport(_handler)
    client = _client(transport)
    posted = await client.post_comment("SSWCI-1", body_wiki="h3. test")
    assert posted.comment_id == "10001"
    assert posted.posted_at.year == 2026
    assert "/rest/api/2/" in str(captured["url"])
    assert captured["body"] == {"body": "h3. test"}


@pytest.mark.asyncio
async def test_post_comment_rejects_empty_body() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(201, json={}))
    client = _client(transport)
    with pytest.raises(PermanentError, match="empty body"):
        await client.post_comment("SSWCI-1", body_wiki="   ")


@pytest.mark.asyncio
async def test_post_comment_rejects_oversized_body() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(201, json={}))
    client = _client(transport)
    huge = "x" * 33_000
    with pytest.raises(PermanentError, match="> Atlassian cap"):
        await client.post_comment("SSWCI-1", body_wiki=huge)


@pytest.mark.asyncio
async def test_basic_auth_header_set() -> None:
    captured: dict[str, object] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["authorization"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"accountId": "u", "emailAddress": "e", "displayName": "d"})

    transport = httpx.MockTransport(_handler)
    client = _client(transport)
    await client.myself()
    auth_header = str(captured["authorization"])
    assert auth_header.startswith("Basic ")
