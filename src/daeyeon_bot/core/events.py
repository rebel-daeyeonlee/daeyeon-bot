"""Domain `Event` — the immutable unit a trigger emits and a handler consumes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import uuid_utils

CURRENT_EVENT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Event:
    """A trigger-emitted, handler-consumed message persisted in the events table.

    `payload` is intentionally `Mapping[str, Any]`: each event `type` defines its
    own schema, validated at the trigger boundary (zod-equivalent: pydantic).
    The dispatcher itself never inspects payloads.
    """

    id: str
    type: str
    schema_version: int
    payload: Mapping[str, Any]
    trace_id: str
    created_at: datetime


def new_event_id() -> str:
    """Generate a UUIDv7 string. Time-sortable; useful as a primary key."""
    return str(uuid_utils.uuid7())


def make_event(
    *,
    type: str,
    payload: Mapping[str, Any],
    created_at: datetime,
    trace_id: str | None = None,
    schema_version: int = CURRENT_EVENT_SCHEMA_VERSION,
) -> Event:
    """Construct an Event with a fresh UUIDv7 id and (optionally) a fresh trace id."""
    return Event(
        id=new_event_id(),
        type=type,
        schema_version=schema_version,
        payload=dict(payload),
        trace_id=trace_id or new_event_id(),
        created_at=created_at,
    )


def migrate(event: Event) -> Event:
    """Lazy event-payload migration hook.

    Phase 0 stub: returns the event unchanged. Real migrators land per-type
    when payload schemas evolve. Called by handlers before validation.
    """
    return event


# `replace` re-exported so callers can build modified copies without importing dataclasses.
copy_with = replace
