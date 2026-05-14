"""In-memory `LokiClient` substitute for unit + integration tests.

Backed by a dict keyed on the stream type. Returns canned `LokiSlice`
results or an error label per test scenario.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from daeyeon_bot.core.jira_triage.types import LokiSlice, LokiStream
from daeyeon_bot.infra.loki import LokiQueryResult


@dataclass(slots=True)
class _Canned:
    lines: tuple[str, ...] = ()
    truncated: bool = False
    error: str | None = None


@dataclass(slots=True)
class FakeLokiClient:
    """Test double for `LokiClient.query_range`."""

    canned: dict[LokiStream, _Canned] = field(default_factory=dict)
    calls: list[tuple[LokiStream, str, datetime, datetime]] = field(default_factory=list)

    def set_response(
        self,
        stream: LokiStream,
        *,
        lines: tuple[str, ...] = (),
        truncated: bool = False,
        error: str | None = None,
    ) -> None:
        self.canned[stream] = _Canned(lines=lines, truncated=truncated, error=error)

    async def query_range(
        self,
        *,
        stream: LokiStream,
        logql: str,
        start: datetime,
        end: datetime,
        limit: int = 5000,
    ) -> LokiQueryResult:
        del limit
        self.calls.append((stream, logql, start, end))
        canned = self.canned.get(stream)
        if canned is None:
            return LokiQueryResult(
                slice=LokiSlice(stream=stream, lines=(), truncated=False),
                error=None,
            )
        if canned.error is not None:
            return LokiQueryResult(slice=None, error=canned.error)
        return LokiQueryResult(
            slice=LokiSlice(stream=stream, lines=canned.lines, truncated=canned.truncated),
            error=None,
        )


__all__ = ["FakeLokiClient"]
