"""Plugin discovery and routing table construction.

Keeps trigger / handler instantiation out of `container.py` so the composition
root stays small and tests can build narrow registries.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta

from daeyeon_bot.app.config import Config, HandlerEntry
from daeyeon_bot.core.errors import ConfigError
from daeyeon_bot.core.manifest import HandlerManifest
from daeyeon_bot.handlers import echo as echo_handler


@dataclass(frozen=True, slots=True)
class HandlerRecord:
    """An instantiated handler ready to dispatch."""

    name: str
    manifest: HandlerManifest
    instance: object  # actual Handler — typing.Protocol does not enforce at runtime here.


@dataclass(slots=True)
class HandlerRegistry:
    """Registry the dispatcher consults to look up a handler by name."""

    by_name: dict[str, HandlerRecord] = field(default_factory=dict[str, HandlerRecord])
    routing: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def register(self, record: HandlerRecord) -> None:
        if record.name in self.by_name:
            raise ConfigError(f"duplicate handler name: {record.name}")
        self.by_name[record.name] = record

    def handlers_for(self, event_type: str) -> list[HandlerRecord]:
        names = self.routing.get(event_type, [])
        return [self.by_name[n] for n in names if n in self.by_name]


def _override_manifest(manifest: HandlerManifest, entry: HandlerEntry) -> HandlerManifest:
    """Apply config overrides on top of the compile-time manifest."""
    kwargs = {}
    if entry.idempotent is not None:
        kwargs["idempotent"] = entry.idempotent
    if entry.dedup_ttl_seconds is not None:
        kwargs["dedup_ttl"] = timedelta(seconds=entry.dedup_ttl_seconds)
    if entry.side_effect_key is not None:
        kwargs["side_effect_key"] = entry.side_effect_key
    if entry.concurrency is not None:
        kwargs["concurrency"] = entry.concurrency
    if entry.accepts is not None:
        kwargs["accepts"] = tuple(entry.accepts)
    if not kwargs:
        return manifest
    return replace(manifest, **kwargs)


def build_handler_registry(config: Config) -> HandlerRegistry:
    """Instantiate enabled handlers from config, applying manifest overrides.

    Phase 1 ships only `echo`. Future handlers register via this function — no
    decorator magic, just an explicit `if name == ...` block. Trade off: O(n)
    branches scale linearly with handler count, but it stays trivial to reason
    about and avoids import-time side effects.
    """
    registry = HandlerRegistry(routing=dict(config.routing))

    for name, entry in config.handlers.items():
        if not entry.enabled:
            continue
        record = instantiate_handler(name, entry)
        registry.register(record)

    return registry


def instantiate_handler(name: str, entry: HandlerEntry) -> HandlerRecord:
    if name == "echo":
        manifest = _override_manifest(echo_handler.MANIFEST, entry)
        return HandlerRecord(
            name=name, manifest=manifest, instance=echo_handler.EchoHandler(manifest)
        )
    raise ConfigError(f"unknown handler in config: {name!r}")
