"""Claude Agent SDK adapter.

Two implementations of the same `ClaudeSession` shape:
    * `FakeClaudeSession` — scripted responses for tests.
    * `RealClaudeSession` — wraps `claude_agent_sdk.ClaudeSDKClient`. The
      OAuth token is passed to the CLI subprocess via an explicit env
      allowlist (not by inheriting the daemon's environment).

Errors map onto `core.errors`:
    * `CLINotFoundError` / `CLIConnectionError` → `TransientError` (retry).
    * `RateLimitEvent` with a non-allowed status → `RateLimitError`.
      `allowed` / `allowed_warning` are informational and logged, not raised.
    * `ProcessError` whose stderr looks auth-related → `AuthError`.
    * Anything else → propagates to the dispatcher's generic catch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import NoReturn, Protocol, runtime_checkable

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    TextBlock,
)

from daeyeon_bot.core.errors import AuthError, RateLimitError, TransientError

_log = structlog.get_logger(__name__)

# RateLimitEvent statuses the SDK emits to inform the client that a request
# went through. Anything outside this set means the request was denied and
# the dispatcher should retry.
_RATE_LIMIT_ALLOWED_STATUSES: frozenset[str] = frozenset({"allowed", "allowed_warning"})

_AUTH_HINTS: tuple[str, ...] = (
    "401",
    "403",
    "unauthorized",
    "invalid_api_key",
    "authentication",
    "oauth",
    "token expired",
    "token revoked",
)


@runtime_checkable
class ClaudeSession(Protocol):
    """The minimal surface a handler uses to talk to Claude."""

    async def __aenter__(self) -> ClaudeSession: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def query(self, prompt: str, *, system: str | None = None) -> str: ...


@dataclass(slots=True)
class FakeClaudeSession:
    """Test double. Returns scripted responses; records calls for assertions.

    Default: echoes the prompt prefixed with `[fake] `. Pass `responses=[...]` to
    play back a sequence; `default` is used after the script is exhausted.
    """

    responses: list[str] = field(default_factory=list[str])
    default: str | None = None
    calls: list[dict[str, str | None]] = field(default_factory=list[dict[str, str | None]])
    closed: bool = False

    async def __aenter__(self) -> FakeClaudeSession:
        self.closed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True

    async def query(self, prompt: str, *, system: str | None = None) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if self.responses:
            return self.responses.pop(0)
        if self.default is not None:
            return self.default
        return f"[fake] {prompt}"


class ClaudeSessionFactory(Protocol):
    """Builds a fresh session per handler invocation."""

    def __call__(self) -> ClaudeSession: ...


@dataclass(slots=True)
class FakeFactory:
    session: FakeClaudeSession

    def __call__(self) -> FakeClaudeSession:
        return self.session


@dataclass(slots=True)
class RealClaudeSession:
    """`ClaudeSDKClient` wrapper that fits the `ClaudeSession` protocol.

    The SDK ties `system_prompt` to `ClaudeAgentOptions` at connect time,
    so we connect lazily on the first `query()` using either the per-call
    `system=` override or `default_system_prompt`. A subsequent query in
    the same session may not change the system prompt — open a new
    session per persona.
    """

    oauth_token: str
    model: str | None
    default_system_prompt: str | None
    _client: ClaudeSDKClient | None = field(default=None, init=False)
    _connected_system: str | None = field(default=None, init=False)
    _entered: bool = field(default=False, init=False)

    async def __aenter__(self) -> RealClaudeSession:
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._entered = False
        client = self._client
        self._client = None
        self._connected_system = None
        if client is None:
            return
        try:
            await client.disconnect()
        except CLIConnectionError:  # pragma: no cover — best-effort
            return

    async def query(self, prompt: str, *, system: str | None = None) -> str:
        if not self._entered:
            raise TransientError("RealClaudeSession used outside of `async with`")
        effective_system = system if system is not None else self.default_system_prompt
        client = await self._ensure_connected(effective_system)
        try:
            await client.query(prompt)
            return await _collect_assistant_text(client)
        except ProcessError as exc:
            _raise_process_error(exc)
        except CLIConnectionError as exc:
            raise TransientError(f"claude CLI connection lost: {exc}") from exc

    async def _ensure_connected(self, system_prompt: str | None) -> ClaudeSDKClient:
        if self._client is not None:
            if system_prompt != self._connected_system:
                raise TransientError(
                    "RealClaudeSession cannot change system prompt mid-session;"
                    " open a new session per persona"
                )
            return self._client
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            env={"CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token},
        )
        client = ClaudeSDKClient(options=options)
        try:
            await client.connect()
        except CLINotFoundError as exc:
            raise TransientError(f"claude CLI not found: {exc}") from exc
        except CLIConnectionError as exc:
            raise TransientError(f"claude CLI connect failed: {exc}") from exc
        self._client = client
        self._connected_system = system_prompt
        return client


async def _collect_assistant_text(client: ClaudeSDKClient) -> str:
    """Drain the response stream and concatenate assistant text blocks.

    Empty results — no `AssistantMessage`/`TextBlock` ever yielded, or only
    whitespace — are surfaced as `TransientError`. Handlers that try to parse
    JSON would otherwise hit "Expecting value: line 1 column 1" and escalate
    to PermanentError; the underlying cause is almost always a transient
    upstream hiccup that the dispatcher's retry/backoff will recover from.
    """
    parts: list[str] = []
    async for message in client.receive_response():
        if isinstance(message, RateLimitEvent):
            status = message.rate_limit_info.status
            if status not in _RATE_LIMIT_ALLOWED_STATUSES:
                raise RateLimitError(f"claude rate limit ({status}): {message}")
            # Informational: request was allowed, just nearing the quota.
            _log.warning(
                "claude.rate_limit_warning",
                status=status,
                rate_limit_type=message.rate_limit_info.rate_limit_type,
                utilization=message.rate_limit_info.utilization,
            )
            continue
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    text = "".join(parts)
    if not text.strip():
        raise TransientError("claude returned no assistant text")
    return text


def _raise_process_error(exc: ProcessError) -> NoReturn:
    detail = str(exc).lower()
    if any(hint in detail for hint in _AUTH_HINTS):
        raise AuthError(f"claude auth failure: {exc}") from exc
    raise TransientError(f"claude process error: {exc}") from exc


def make_real_factory(
    *, oauth_token: str, model: str | None, default_system_prompt: str | None
) -> Callable[[], RealClaudeSession]:
    """Closure that builds a fresh `RealClaudeSession` per dispatch."""

    def _factory() -> RealClaudeSession:
        return RealClaudeSession(
            oauth_token=oauth_token,
            model=model,
            default_system_prompt=default_system_prompt,
        )

    return _factory


__all__ = [
    "ClaudeSession",
    "ClaudeSessionFactory",
    "FakeClaudeSession",
    "FakeFactory",
    "RealClaudeSession",
    "make_real_factory",
]
