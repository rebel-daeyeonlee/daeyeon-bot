"""Async wrapper for Grafana Loki's HTTP query API.

Used by the `jira_triage` handler to pull fwlog/smclog/kernel/syslog
slices for the run window. The cluster Loki at
`http://loki.ssw.rbln.in` is unauthenticated and cluster-internal, so
there's no `Authorization` header — see
`specs/002-jira-triage-bot/contracts/loki-query-surface.md`.

Hostname is REQUIRED on every query — the wrapper builds the LogQL stream
selector so the caller can't accidentally issue a query without a
hostname filter (which would scrape unrelated streams across the
cluster).

Error policy: 4xx/5xx/timeout do NOT raise — they return an empty
`LokiSlice` with `error` populated. The handler treats Loki outages as a
partial-data condition, not a triage-killing failure (per FR-013).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import httpx

from daeyeon_bot.core.jira_triage.types import LokiSlice, LokiStream

_MAX_RETRIES_5XX = 3
_BASE_BACKOFF_S = 0.5


@dataclass(frozen=True, slots=True)
class LokiQueryResult:
    """Result of one `query_range` call. Either a slice or an error label."""

    slice: LokiSlice | None
    error: str | None  # e.g. "5xx" / "429" / "timeout" / "dns_failed:<host>"


class LokiQueryBuilder:
    """Pure LogQL selector builders. No I/O."""

    @staticmethod
    def fwlog_for(*, host_ip: str, tc_name: str) -> str:
        return f'{{job="regression-fwlog", hostname="{host_ip}", test_name="{_esc(tc_name)}"}}'

    @staticmethod
    def smclog_for(*, host_ip: str, tc_name: str) -> str:
        return f'{{job="regression-smclog", hostname="{host_ip}", test_name="{_esc(tc_name)}"}}'

    @staticmethod
    def kernel_for(*, host_name: str, template: str) -> str:
        return template.replace("{host}", _esc(host_name))

    @staticmethod
    def syslog_for(*, host_name: str, template: str) -> str:
        return template.replace("{host}", _esc(host_name))


class LokiClient:
    """Thin httpx wrapper. One instance per daemon."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float = 30.0,
        per_stream_max_bytes: int = 1_048_576,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._per_stream_max_bytes = per_stream_max_bytes
        self._http = http

    async def query_range(  # noqa: PLR0911 — explicit branch per error class
        self,
        *,
        stream: LokiStream,
        logql: str,
        start: datetime,
        end: datetime,
        limit: int = 5000,
    ) -> LokiQueryResult:
        """Run a `/loki/api/v1/query_range` and return a `LokiSlice` or error label."""
        if "hostname=" not in logql:
            raise ValueError(f"loki: refusing query without hostname filter: {logql!r}")
        if end <= start:
            raise ValueError(f"loki: end ({end}) must be > start ({start})")

        start_ns = int(start.timestamp() * 1_000_000_000)
        end_ns = int(end.timestamp() * 1_000_000_000)
        params = {
            "query": logql,
            "start": str(start_ns),
            "end": str(end_ns),
            "direction": "forward",
            "limit": str(limit),
        }
        url = f"{self._base_url}/loki/api/v1/query_range"

        for attempt in range(_MAX_RETRIES_5XX):
            try:
                response = await self._do_request(url, params)
            except httpx.TimeoutException:
                return LokiQueryResult(slice=None, error="timeout")
            except httpx.RequestError as exc:
                return LokiQueryResult(slice=None, error=f"network:{exc}")

            status = response.status_code
            if 200 <= status < 300:
                try:
                    data = response.json()
                except ValueError:
                    return LokiQueryResult(slice=None, error="non-json")
                lines, truncated = _extract_lines(data, self._per_stream_max_bytes)
                return LokiQueryResult(
                    slice=LokiSlice(stream=stream, lines=lines, truncated=truncated),
                    error=None,
                )
            if status == 429:
                if attempt + 1 < _MAX_RETRIES_5XX:
                    await asyncio.sleep(_BASE_BACKOFF_S * (2**attempt))
                    continue
                return LokiQueryResult(slice=None, error="429")
            if 500 <= status < 600:
                if attempt + 1 < _MAX_RETRIES_5XX:
                    await asyncio.sleep(_BASE_BACKOFF_S * (2**attempt))
                    continue
                return LokiQueryResult(slice=None, error=f"5xx:{status}")
            # 4xx other than 429 — fail fast, no retry.
            return LokiQueryResult(slice=None, error=f"4xx:{status}")
        # Loop fell through (defensive — shouldn't happen).
        return LokiQueryResult(slice=None, error="exhausted")

    async def _do_request(
        self,
        url: str,
        params: dict[str, str],
    ) -> httpx.Response:
        if self._http is not None:
            return await self._http.get(url, params=params, timeout=self._timeout)
        async with httpx.AsyncClient() as client:
            return await client.get(url, params=params, timeout=self._timeout)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _esc(value: str) -> str:
    """Escape `"` and `\\` inside a Loki LogQL string-literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _extract_lines(
    data: object,
    max_bytes: int,
) -> tuple[tuple[str, ...], bool]:
    """Pull the `data.result[*].values[*][1]` lines, capped at `max_bytes`.

    Returns `(lines, truncated)`. `truncated=True` when we stopped early
    because the byte budget was exceeded.
    """
    if not isinstance(data, dict):
        return ((), False)
    body = cast("dict[str, Any]", data).get("data")
    if not isinstance(body, dict):
        return ((), False)
    result_block = cast("dict[str, Any]", body).get("result")
    if not isinstance(result_block, list):
        return ((), False)

    lines: list[str] = []
    used = 0
    truncated = False
    for stream_block in cast("list[Any]", result_block):
        if not isinstance(stream_block, dict):
            continue
        values = cast("dict[str, Any]", stream_block).get("values")
        if not isinstance(values, list):
            continue
        for pair in cast("list[Any]", values):
            if not isinstance(pair, list) or len(pair) < 2:  # type: ignore[arg-type]
                continue
            pair_typed = cast("list[Any]", pair)
            line = str(pair_typed[1])
            line_size = len(line.encode("utf-8", errors="ignore"))
            if used + line_size > max_bytes:
                truncated = True
                break
            lines.append(line)
            used += line_size
        if truncated:
            break
    return (tuple(lines), truncated)


__all__ = [
    "LokiClient",
    "LokiQueryBuilder",
    "LokiQueryResult",
]
