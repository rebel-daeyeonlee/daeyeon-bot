"""Claude Agent SDK adapter.

Two session shapes live here:

    * `ClaudeSession`      — text in, text out. No tools, no filesystem.
    * `ClaudeAgentSession` — pinned to a `cwd` with a tool allowlist, so the
      model can read and edit a repository (feature 004, `pr_autofix`).

Implementations of the plain `ClaudeSession` shape:
    * `FakeClaudeSession` — scripted responses for tests.
    * `RealClaudeSession` — wraps `claude_agent_sdk.ClaudeSDKClient`. The
      OAuth token is passed to the CLI subprocess via an explicit env
      allowlist (not by inheriting the daemon's environment).

Errors map onto `core.errors`:
    * `CLINotFoundError` / `CLIConnectionError` → `TransientError` (retry).
    * `RateLimitEvent` with a non-allowed status → `RateLimitError`.
      `allowed` / `allowed_warning` are informational and logged, not raised.
    * `ProcessError` whose stderr looks auth-related → `AuthError`.
    * An API error *envelope* arriving as the assistant's text → `AuthError` /
      `RateLimitError` / `TransientError` by `error.type`. See
      `_raise_if_api_error`; this path exists because a rejected upstream call
      does not always reach us as an exception.
    * Anything else → propagates to the dispatcher's generic catch.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import NoReturn, Protocol, cast, runtime_checkable

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

# `error.type` values from the API's error envelope that mean "fix the
# credential", not "retry later". Matched exactly against `error.type` — never
# against `error.message`, because a false positive here halts the daemon with
# exit 78 and a supervisor that refuses to restart it.
_AUTH_ERROR_TYPES: frozenset[str] = frozenset(
    {"authentication_error", "permission_error", "invalid_request_error_auth"}
)

# The CLI prints this (not JSON) when it cannot authenticate at all, e.g.
# "Failed to authenticate. API Error: 401 OAuth access token has been revoked."
_CLI_AUTH_FAILURE_PREFIX = "failed to authenticate"


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
        except Exception as disconnect_exc:  # pragma: no cover — best-effort teardown
            # Broad catch: __aexit__ must not mask the original exception that
            # was unwinding the `async with`. Any disconnect failure is
            # logged and swallowed so the caller's exception (if any) wins.
            _log.warning("claude.disconnect_failed", error=str(disconnect_exc))

    async def query(self, prompt: str, *, system: str | None = None) -> str:
        if not self._entered:
            raise TransientError("RealClaudeSession used outside of `async with`")
        effective_system = system if system is not None else self.default_system_prompt
        client = await self._ensure_connected(effective_system)
        try:
            await client.query(prompt)
            return await _collect_assistant_text(client, prompt_chars=len(prompt))
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


async def _collect_assistant_text(client: ClaudeSDKClient, *, prompt_chars: int) -> str:
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
        # `prompt_chars` is the only context the operator gets when triaging
        # repeated empties — a near-zero prompt is a serializer bug, a
        # near-cap prompt suggests we're hitting an SDK truncation path.
        _log.warning("claude.empty_assistant_text", prompt_chars=prompt_chars)
        raise TransientError("claude returned no assistant text")
    _raise_if_api_error(text)
    return text


def _raise_if_api_error(text: str) -> None:
    """Raise when the CLI handed us an API error instead of a model reply.

    A rejected upstream call does not always reach us as an exception: the CLI
    can surface it as the assistant's own *text*, so the SDK sees a well-formed
    turn and nothing in the `ProcessError` path fires. Left alone that payload
    flows into a handler's `json.loads`, parses cleanly, and dies as a schema
    violation → `DeadLetter` — burning one event per attempt instead of halting
    the daemon. Observed 2026-08-04..06: 108 `pr_review` rows dead-lettered on
    `{"type":"error","error":{"type":"authentication_error",...}}` from a
    revoked token, while `ops doctor` reported the token `ok`.

    Only a reply that is *entirely* the error counts. A review that happens to
    quote such a payload (plausible — this bot reviews code that talks to APIs)
    keeps flowing.
    """
    stripped = text.strip()
    if stripped.lower().startswith(_CLI_AUTH_FAILURE_PREFIX):
        _log.error("claude.cli_auth_failure", detail=stripped[:400])
        raise AuthError(f"claude auth failure: {stripped[:400]}")
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return
    try:
        payload: object = json.loads(stripped)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    envelope = cast("dict[str, object]", payload)
    if envelope.get("type") != "error":
        return

    inner = envelope.get("error")
    err_type = ""
    message = ""
    if isinstance(inner, dict):
        inner_fields = cast("dict[str, object]", inner)
        err_type = str(inner_fields.get("type") or "")
        message = str(inner_fields.get("message") or "")
    if err_type and message:
        detail = f"{err_type}: {message}"
    else:
        detail = err_type or stripped[:200]

    _log.error("claude.api_error_envelope", error_type=err_type, detail=detail[:400])
    if err_type in _AUTH_ERROR_TYPES:
        raise AuthError(f"claude auth failure: {detail}")
    if err_type == "rate_limit_error":
        raise RateLimitError(f"claude rate limit: {detail}")
    raise TransientError(f"claude api error: {detail}")


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


# ── Tool-enabled agent sessions (feature 004) ─────────────────────────────
#
# `ClaudeSession` above is text-in / text-out: the model reads a rendered
# prompt and writes a reply. That is the right shape for review / triage
# handlers, which never touch the filesystem.
#
# `pr_autofix` needs the other shape — the model must READ the repository and
# EDIT it. That means a session pinned to a working directory with a tool
# allowlist. The blast radius is bounded by three things, in this order:
#   1. `cwd` — the SDK subprocess starts in the throwaway workspace clone;
#   2. `allowed_tools` / `disallowed_tools` — no WebFetch, no Task fan-out;
#   3. `setting_sources=None` (the SDK default) — the operator's own
#      ~/.claude settings, hooks and MCP servers are NOT loaded, so the
#      daemon's agent can't inherit interactive credentials.
# The handler adds a fourth: it inspects `git diff` before committing and
# refuses protected paths / oversized diffs regardless of what the agent did.

# Read + edit + local shell. `Bash` is included because a real fix often needs
# to run the formatter or a single test; the handler's post-hoc diff gate is
# what keeps that honest. `WebFetch`/`WebSearch`/`Task` are excluded — a fix
# agent that reaches the network or fans out to sub-agents is out of contract.
DEFAULT_AGENT_TOOLS: tuple[str, ...] = (
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    "Bash",
)
DEFAULT_AGENT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "WebFetch",
    "WebSearch",
    "Task",
    "NotebookEdit",
)


@runtime_checkable
class ClaudeAgentSession(Protocol):
    """A `ClaudeSession` that also has tools and a working directory."""

    async def __aenter__(self) -> ClaudeAgentSession: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def query(self, prompt: str, *, system: str | None = None) -> str: ...


class ClaudeAgentSessionFactory(Protocol):
    """Builds a fresh tool-enabled session bound to `cwd`."""

    def __call__(self, *, cwd: Path) -> ClaudeAgentSession: ...


@dataclass(slots=True)
class FakeClaudeAgentSession:
    """Test double for `ClaudeAgentSession`.

    Records the `cwd` it was built for and every prompt. `on_query` lets a test
    simulate the side effect a real agent would have (writing files into the
    workspace) before the scripted text is returned.
    """

    cwd: Path
    responses: list[str] = field(default_factory=list[str])
    default: str | None = None
    calls: list[dict[str, str | None]] = field(default_factory=list[dict[str, str | None]])
    on_query: Callable[[Path], None] | None = None
    closed: bool = False

    async def __aenter__(self) -> FakeClaudeAgentSession:
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
        if self.on_query is not None:
            self.on_query(self.cwd)
        if self.responses:
            return self.responses.pop(0)
        if self.default is not None:
            return self.default
        return f"[fake-agent] {prompt}"


@dataclass(slots=True)
class RealClaudeAgentSession:
    """`ClaudeSDKClient` wrapper with `cwd` + a tool allowlist.

    `permission_mode="bypassPermissions"` is required, not a shortcut: the
    daemon is headless, so any interactive permission prompt would hang the
    handler until its timeout. The allowlist above plus the handler's diff gate
    are what stand in for the human at the prompt. Never point this at anything
    but a throwaway workspace clone (`infra/git_workspace.py` enforces that
    the path sits under the configured workspace root).
    """

    oauth_token: str
    model: str | None
    cwd: Path
    allowed_tools: tuple[str, ...] = DEFAULT_AGENT_TOOLS
    disallowed_tools: tuple[str, ...] = DEFAULT_AGENT_DISALLOWED_TOOLS
    max_turns: int | None = None
    _client: ClaudeSDKClient | None = field(default=None, init=False)
    _connected_system: str | None = field(default=None, init=False)
    _entered: bool = field(default=False, init=False)

    async def __aenter__(self) -> RealClaudeAgentSession:
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
        except Exception as disconnect_exc:  # pragma: no cover — best-effort teardown
            _log.warning("claude_agent.disconnect_failed", error=str(disconnect_exc))

    async def query(self, prompt: str, *, system: str | None = None) -> str:
        if not self._entered:
            raise TransientError("RealClaudeAgentSession used outside of `async with`")
        client = await self._ensure_connected(system)
        try:
            await client.query(prompt)
            return await _collect_assistant_text(client, prompt_chars=len(prompt))
        except ProcessError as exc:
            _raise_process_error(exc)
        except CLIConnectionError as exc:
            raise TransientError(f"claude CLI connection lost: {exc}") from exc

    async def _ensure_connected(self, system_prompt: str | None) -> ClaudeSDKClient:
        if self._client is not None:
            if system_prompt != self._connected_system:
                raise TransientError(
                    "RealClaudeAgentSession cannot change system prompt mid-session;"
                    " open a new session per persona"
                )
            return self._client
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            cwd=self.cwd,
            allowed_tools=list(self.allowed_tools),
            disallowed_tools=list(self.disallowed_tools),
            permission_mode="bypassPermissions",
            max_turns=self.max_turns,
            # Do NOT inherit the operator's ~/.claude settings, hooks, or MCP
            # servers into a daemon-driven agent. None is the SDK default; it
            # is spelled out here because the isolation is load-bearing.
            setting_sources=None,
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


def make_real_agent_factory(
    *,
    oauth_token: str,
    model: str | None,
    max_turns: int | None = None,
) -> Callable[..., RealClaudeAgentSession]:
    """Closure that builds a fresh tool-enabled session per workspace."""

    def _factory(*, cwd: Path) -> RealClaudeAgentSession:
        return RealClaudeAgentSession(
            oauth_token=oauth_token,
            model=model,
            cwd=cwd,
            max_turns=max_turns,
        )

    return _factory


__all__ = [
    "DEFAULT_AGENT_DISALLOWED_TOOLS",
    "DEFAULT_AGENT_TOOLS",
    "ClaudeAgentSession",
    "ClaudeAgentSessionFactory",
    "ClaudeSession",
    "ClaudeSessionFactory",
    "FakeClaudeAgentSession",
    "FakeClaudeSession",
    "FakeFactory",
    "RealClaudeAgentSession",
    "RealClaudeSession",
    "make_real_agent_factory",
    "make_real_factory",
]
