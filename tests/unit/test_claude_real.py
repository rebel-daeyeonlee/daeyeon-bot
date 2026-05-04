"""RealClaudeSession adapter — error mapping + assistant-text collection.

We don't spawn the real Claude Code CLI here; we monkeypatch
`ClaudeSDKClient` with a stub that yields scripted messages and exposes
the `connect/disconnect/query/receive_response` surface the adapter uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    TextBlock,
)

from daeyeon_bot.core.errors import AuthError, RateLimitError, TransientError
from daeyeon_bot.infra import claude as claude_mod


@dataclass
class _StubClient:
    """Stub matching the slice of `ClaudeSDKClient` that the adapter touches."""

    on_connect: BaseException | None = None
    on_query: BaseException | None = None
    scripted_messages: list[object] = field(default_factory=list[object])
    queries: list[str] = field(default_factory=list[str])
    connected: bool = False
    disconnected: bool = False

    async def connect(self) -> None:
        if self.on_connect is not None:
            raise self.on_connect
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)
        if self.on_query is not None:
            raise self.on_query

    async def receive_response(self) -> AsyncIterator[object]:
        for msg in self.scripted_messages:
            yield msg


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    """Replace ClaudeSDKClient with a single shared stub for the test."""
    instance = _StubClient()

    def _factory(*_args: object, **_kwargs: object) -> _StubClient:
        return instance

    monkeypatch.setattr(claude_mod, "ClaudeSDKClient", _factory)
    return instance


def _session() -> claude_mod.RealClaudeSession:
    return claude_mod.RealClaudeSession(
        oauth_token="tok-abc",
        model="claude-opus-4-7",
        default_system_prompt="You are helpful.",
    )


async def test_query_concatenates_assistant_text(stub: _StubClient) -> None:
    stub.scripted_messages = [
        AssistantMessage(
            content=[TextBlock(text="hello "), TextBlock(text="world")],
            model="m",
            parent_tool_use_id=None,
            error=None,
            usage=None,
            message_id="mid",
            stop_reason=None,
            session_id="s",
            uuid="u",
        ),
    ]
    async with _session() as session:
        out = await session.query("hi")
    assert out == "hello world"
    assert stub.queries == ["hi"]
    assert stub.connected
    assert stub.disconnected


async def test_per_call_system_override_is_passed_to_sdk(
    stub: _StubClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-query `system=` flows into ClaudeAgentOptions at lazy connect."""
    captured: dict[str, object] = {}

    def _factory(*, options: object, **_kwargs: object) -> _StubClient:
        captured["options"] = options
        return stub

    monkeypatch.setattr(claude_mod, "ClaudeSDKClient", _factory)
    stub.scripted_messages = [
        AssistantMessage(
            content=[TextBlock(text="ok")],
            model="m",
            parent_tool_use_id=None,
            error=None,
            usage=None,
            message_id="mid",
            stop_reason=None,
            session_id="s",
            uuid="u",
        ),
    ]
    async with _session() as session:
        await session.query("hi", system="persona-A")
    options = cast("claude_mod.ClaudeAgentOptions", captured["options"])
    assert options.system_prompt == "persona-A"


async def test_changing_system_mid_session_raises(stub: _StubClient) -> None:
    """Same session, two queries with different system → TransientError."""
    stub.scripted_messages = [
        AssistantMessage(
            content=[TextBlock(text="ok")],
            model="m",
            parent_tool_use_id=None,
            error=None,
            usage=None,
            message_id="mid",
            stop_reason=None,
            session_id="s",
            uuid="u",
        ),
    ]
    async with _session() as session:
        await session.query("hi", system="persona-A")
        with pytest.raises(TransientError, match="cannot change system prompt"):
            await session.query("hi", system="persona-B")


async def test_connect_failure_maps_to_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _StubClient(on_connect=CLINotFoundError("not found"))

    def _factory(*_args: object, **_kwargs: object) -> _StubClient:
        return instance

    monkeypatch.setattr(claude_mod, "ClaudeSDKClient", _factory)
    async with _session() as session:
        with pytest.raises(TransientError, match="claude CLI not found"):
            await session.query("hi")


async def test_rate_limit_event_maps_to_rate_limit_error(stub: _StubClient) -> None:
    info = RateLimitInfo(
        status="rejected",
        resets_at=None,
        rate_limit_type="five_hour",
        utilization=1.0,
        overage_status=None,
        overage_resets_at=None,
        overage_disabled_reason=None,
        raw={},
    )
    event = RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")
    stub.scripted_messages = [cast("object", event)]
    async with _session() as session:
        with pytest.raises(RateLimitError):
            await session.query("hi")


async def test_rate_limit_allowed_warning_does_not_raise(stub: _StubClient) -> None:
    """`allowed_warning` is informational — request went through."""
    info = RateLimitInfo(
        status="allowed_warning",
        resets_at=1777890600,
        rate_limit_type="five_hour",
        utilization=0.93,
        overage_status=None,
        overage_resets_at=None,
        overage_disabled_reason=None,
        raw={},
    )
    rate_event = RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")
    text_msg = AssistantMessage(
        content=[TextBlock(text="ok")],
        model="m",
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id="mid",
        stop_reason=None,
        session_id="s",
        uuid="u",
    )
    stub.scripted_messages = [cast("object", rate_event), cast("object", text_msg)]
    async with _session() as session:
        out = await session.query("hi")
    assert out == "ok"


async def test_auth_keyword_in_process_error_maps_to_auth_error(stub: _StubClient) -> None:
    stub.on_query = ProcessError("HTTP 401 unauthorized: token expired")
    async with _session() as session:
        with pytest.raises(AuthError):
            await session.query("hi")


async def test_other_process_error_maps_to_transient(stub: _StubClient) -> None:
    stub.on_query = ProcessError("CLI exited with code 1: model unavailable")
    async with _session() as session:
        with pytest.raises(TransientError):
            await session.query("hi")


async def test_query_outside_async_with_raises(stub: _StubClient) -> None:
    session = _session()
    with pytest.raises(TransientError, match="outside of"):
        await session.query("hi")


async def test_empty_response_raises_transient(stub: _StubClient) -> None:
    """No AssistantMessage / TextBlock yielded → TransientError, not silent ''.

    Without this guard, empty responses cascade into PermanentError on the
    handler side (json.loads("") → "Expecting value: line 1 column 1").
    """
    stub.scripted_messages = []
    async with _session() as session:
        with pytest.raises(TransientError, match="no assistant text"):
            await session.query("hi")


async def test_whitespace_only_response_raises_transient(stub: _StubClient) -> None:
    stub.scripted_messages = [
        AssistantMessage(
            content=[TextBlock(text="   \n  ")],
            model="m",
            parent_tool_use_id=None,
            error=None,
            usage=None,
            message_id="mid",
            stop_reason=None,
            session_id="s",
            uuid="u",
        ),
    ]
    async with _session() as session:
        with pytest.raises(TransientError, match="no assistant text"):
            await session.query("hi")


def test_make_real_factory_builds_sessions() -> None:
    factory = claude_mod.make_real_factory(
        oauth_token="tok",
        model="m",
        default_system_prompt="sp",
    )
    session = factory()
    assert isinstance(session, claude_mod.RealClaudeSession)
    assert session.oauth_token == "tok"
    assert session.model == "m"
    assert session.default_system_prompt == "sp"
