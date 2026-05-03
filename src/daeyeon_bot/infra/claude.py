"""Claude Agent SDK adapter.

Phase 1: AsyncContextManager protocol + a `FakeClaudeSession` for tests.
Phase 4: real SDK wiring with explicit env allowlist for the subprocess.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol, runtime_checkable


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


@asynccontextmanager
async def open_session() -> AsyncGenerator[ClaudeSession, None]:
    """Phase 4 entry point — wires the real `claude_agent_sdk`. Phase 1: NotImplemented."""
    raise NotImplementedError("Phase 4: instantiate claude_agent_sdk client")
    yield  # pragma: no cover
