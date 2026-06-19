"""Slack Web API wrapper (feature 003) — httpx MockTransport, no network."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from daeyeon_bot.core.errors import AuthError, ConfigError, RateLimitError
from daeyeon_bot.infra.slack import SlackClient


def _client(handler: Any, *, allowed: frozenset[str] = frozenset({"C1", "C_DRY"})) -> SlackClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return SlackClient(token="xoxb-test", allowed_post_channels=allowed, http=http)


async def test_auth_test_parses_identity() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "team": "Rebellions Inc.",
                "user": "devsysswtest",
                "user_id": "U069",
                "bot_id": "B069",
                "url": "https://x.slack.com/",
            },
        )

    slack = _client(handler)
    ident = await slack.auth_test()
    assert ident.user_id == "U069"
    assert ident.bot_id == "B069"


async def test_history_pagination_cursor() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"ts": "100.1", "text": "a"}, {"ts": "100.2", "text": "b"}],
                "response_metadata": {"next_cursor": "CUR2"},
            },
        )

    slack = _client(handler)
    page = await slack.history("C1", oldest="99.0")
    assert [m["ts"] for m in page.messages] == ["100.1", "100.2"]
    assert page.next_cursor == "CUR2"


async def test_post_message_customize_and_thread() -> None:
    seen: dict[str, list[str]] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(req.content.decode()))
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "200.5"})

    slack = _client(handler)
    res = await slack.post_message(
        "C1", "hello", thread_ts="100.1", username="CI Triage", icon_emoji=":robot_face:"
    )
    assert res.ts == "200.5"
    assert seen["thread_ts"] == ["100.1"]
    assert seen["username"] == ["CI Triage"]
    assert seen["channel"] == ["C1"]


async def test_post_message_allowlist_rejection_no_http() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("post_message must not hit HTTP for a disallowed channel")

    slack = _client(handler, allowed=frozenset({"C1"}))
    with pytest.raises(ConfigError):
        await slack.post_message("C_OTHER", "nope")


async def test_auth_error_mapping() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "token_revoked"})

    slack = _client(handler)
    with pytest.raises(AuthError):
        await slack.auth_test()


async def test_ratelimited_mapping() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"}, json={"ok": False})

    slack = _client(handler)
    with pytest.raises(RateLimitError):
        await slack.history("C1")
