"""LokiClient + LokiQueryBuilder — T023 tests."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from daeyeon_bot.infra.loki import LokiClient, LokiQueryBuilder


def _client(handler: httpx.MockTransport, **kwargs: object) -> LokiClient:
    return LokiClient(
        base_url="http://loki.ssw.rbln.in",
        timeout_s=5.0,
        http=httpx.AsyncClient(transport=handler),
        **kwargs,  # type: ignore[arg-type]
    )


def _window() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 5, 13, 6, 50, tzinfo=UTC),
        datetime(2026, 5, 13, 7, 10, tzinfo=UTC),
    )


# ── Query builder ────────────────────────────────────────────────────────────


def test_builder_fwlog_uses_ip_label() -> None:
    out = LokiQueryBuilder.fwlog_for(host_ip="10.0.0.5", tc_name="TC-0033-x")
    assert 'hostname="10.0.0.5"' in out
    assert 'test_name="TC-0033-x"' in out
    assert 'job="regression-fwlog"' in out


def test_builder_smclog_uses_ip_label() -> None:
    out = LokiQueryBuilder.smclog_for(host_ip="10.0.0.5", tc_name="TC-0033-x")
    assert 'job="regression-smclog"' in out
    assert 'hostname="10.0.0.5"' in out


def test_builder_kernel_substitutes_host_name() -> None:
    template = '{hostname="{host}", job=~"varlogs|systemd-journal", filename=~".*kern.*"}'
    out = LokiQueryBuilder.kernel_for(host_name="ssw-giga-02", template=template)
    assert 'hostname="ssw-giga-02"' in out


def test_builder_escapes_quotes_in_tc_name() -> None:
    out = LokiQueryBuilder.fwlog_for(host_ip="10.0.0.5", tc_name='TC-"weird"-x')
    assert '\\"weird\\"' in out


# ── query_range — wrapper behavior ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_range_refuses_without_hostname() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    client = _client(transport)
    start, end = _window()
    with pytest.raises(ValueError, match="hostname"):
        await client.query_range(stream="kernel", logql='{job="something"}', start=start, end=end)


@pytest.mark.asyncio
async def test_query_range_refuses_zero_window() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    client = _client(transport)
    now = datetime(2026, 5, 13, 7, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"end .* must be > start"):
        await client.query_range(stream="kernel", logql='{hostname="x"}', start=now, end=now)


@pytest.mark.asyncio
async def test_query_range_success_returns_slice() -> None:
    payload = {
        "data": {
            "result": [
                {
                    "stream": {"hostname": "10.0.0.5"},
                    "values": [
                        ["1747119288924242000", "[fwlog] FW HALT err_code=0x10007"],
                        ["1747119289001234000", "[fwlog] cmd_queue full"],
                    ],
                }
            ]
        }
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    client = _client(transport)
    start, end = _window()
    result = await client.query_range(
        stream="fwlog",
        logql=LokiQueryBuilder.fwlog_for(host_ip="10.0.0.5", tc_name="TC-1"),
        start=start,
        end=end,
    )
    assert result.error is None
    assert result.slice is not None
    assert result.slice.stream == "fwlog"
    assert len(result.slice.lines) == 2
    assert "FW HALT" in result.slice.lines[0]


@pytest.mark.asyncio
async def test_query_range_truncates_at_byte_cap() -> None:
    big_line = "x" * 1000
    payload = {
        "data": {
            "result": [
                {
                    "stream": {"hostname": "10.0.0.5"},
                    "values": [[str(i), big_line] for i in range(20)],
                }
            ]
        }
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    client = _client(transport, per_stream_max_bytes=5_000)
    start, end = _window()
    result = await client.query_range(
        stream="fwlog",
        logql=LokiQueryBuilder.fwlog_for(host_ip="10.0.0.5", tc_name="TC-1"),
        start=start,
        end=end,
    )
    assert result.slice is not None
    assert result.slice.truncated is True
    assert len(result.slice.lines) <= 5  # <= 5 lines x 1000 bytes <= cap


@pytest.mark.asyncio
async def test_query_range_4xx_returns_error_label() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(400, text="bad query"))
    client = _client(transport)
    start, end = _window()
    result = await client.query_range(
        stream="fwlog",
        logql=LokiQueryBuilder.fwlog_for(host_ip="10.0.0.5", tc_name="TC-1"),
        start=start,
        end=end,
    )
    assert result.slice is None
    assert result.error is not None
    assert result.error.startswith("4xx")


@pytest.mark.asyncio
async def test_query_range_429_retries_then_gives_up() -> None:
    counter = {"n": 0}

    def _handler(req: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    transport = httpx.MockTransport(_handler)
    client = _client(transport)
    start, end = _window()
    result = await client.query_range(
        stream="fwlog",
        logql=LokiQueryBuilder.fwlog_for(host_ip="10.0.0.5", tc_name="TC-1"),
        start=start,
        end=end,
    )
    assert result.slice is None
    assert result.error == "429"
    assert counter["n"] == 3  # exhausted MAX_RETRIES


@pytest.mark.asyncio
async def test_query_range_5xx_retries_then_gives_up() -> None:
    counter = {"n": 0}

    def _handler(req: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(502, text="bad gateway")

    transport = httpx.MockTransport(_handler)
    client = _client(transport)
    start, end = _window()
    result = await client.query_range(
        stream="kernel",
        logql='{hostname="ssw-giga-02", job="varlogs"}',
        start=start,
        end=end,
    )
    assert result.slice is None
    assert result.error is not None
    assert result.error.startswith("5xx")
    assert counter["n"] == 3


@pytest.mark.asyncio
async def test_query_range_timeout_returns_error_label() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=req)

    transport = httpx.MockTransport(_handler)
    client = _client(transport)
    start, end = _window()
    result = await client.query_range(
        stream="kernel",
        logql='{hostname="ssw-giga-02"}',
        start=start,
        end=end,
    )
    assert result.slice is None
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_query_range_sends_ns_timestamps() -> None:
    captured: dict[str, object] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["start"] = req.url.params.get("start")
        captured["end"] = req.url.params.get("end")
        return httpx.Response(200, json={"data": {"result": []}})

    transport = httpx.MockTransport(_handler)
    client = _client(transport)
    start = datetime(2026, 5, 13, 6, 0, tzinfo=UTC)
    end = datetime(2026, 5, 13, 7, 0, tzinfo=UTC)
    await client.query_range(
        stream="fwlog",
        logql=LokiQueryBuilder.fwlog_for(host_ip="10.0.0.5", tc_name="TC"),
        start=start,
        end=end,
    )
    # Each timestamp is nanoseconds → 19 digits for 2026 epoch.
    assert isinstance(captured["start"], str) and len(captured["start"]) >= 18
    # Make sure it parses back to the same seconds.
    assert int(captured["start"]) // 1_000_000_000 == int(start.timestamp())
