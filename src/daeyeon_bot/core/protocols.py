"""Plugin interfaces (PEP 544 Protocols).

Triggers and handlers are looked up by name from the container. They depend
only on the abstractions in this module — never on concrete infra.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, runtime_checkable

from daeyeon_bot.core.events import Event
from daeyeon_bot.core.manifest import HandlerManifest, TriggerManifest
from daeyeon_bot.core.results import HandlerResult
from daeyeon_bot.core.time import Clock

EmitFn = Callable[[Event], Awaitable[None]]


@runtime_checkable
class TriggerContext(Protocol):
    clock: Clock


@runtime_checkable
class HandlerContext(Protocol):
    clock: Clock
    trace_id: str
    # Factory yields a fresh Claude session per handler call. Kept as a callable
    # rather than the session itself so handlers can choose not to call Claude.
    claude_session_factory: Callable[[], object]


@runtime_checkable
class Trigger(Protocol):
    manifest: TriggerManifest

    async def run(self, emit: EmitFn, ctx: TriggerContext) -> None: ...


E_contra = TypeVar("E_contra", bound=Event, contravariant=True)


class Handler(Protocol[E_contra]):
    manifest: HandlerManifest

    async def handle(self, event: E_contra, ctx: HandlerContext) -> HandlerResult: ...


@runtime_checkable
class Storage(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class Outbox(Protocol):
    async def insert(self, event: Event, handler: str) -> int: ...

    async def claim_one(self, *, claimed_by: str) -> int | None: ...

    async def settle(self, outbox_id: int, result: HandlerResult) -> None: ...
