# Contract — Loki query surface used by the bot

This file pins which LogQL queries the bot issues, the label conventions
they rely on, and the bytes/lines budget per stream.

The base URL is `[loki].base_url`, default `http://loki.ssw.rbln.in`
(cluster-internal, **unauthenticated** — confirmed by Rebellions' Loki
operator on 2026-05-13). No `Authorization` header. All HTTP via
`httpx.AsyncClient`.

---

## Endpoint used (1 total — read-only)

### `GET /loki/api/v1/query_range`

```
GET /loki/api/v1/query_range
   ?query={logql}
   &start={start_ns}            # unix nanoseconds
   &end={end_ns}                # unix nanoseconds
   &direction=forward
   &limit=5000                  # Loki default
```

**Read shape (subset)**:
```jsonc
{
  "status": "success",
  "data": {
    "resultType": "streams",
    "result": [
      {
        "stream": { "job": "regression-fwlog", "hostname": "10.0.x.y", "test_name": "TC-0033-...", "device": "rbln0" },
        "values": [
          ["1747119288924242000", "[fwlog] FW HALT err_code=0x10007 ..."],
          ["1747119289001234000", "[fwlog] cmd_queue full ..."]
        ]
      }
    ]
  }
}
```

The bot extracts the `values` (list of `[ns_timestamp_str, log_line]`)
from each stream, applies the per-stream cap, and returns a
`LokiSlice` to the handler.

---

## Per-triage queries (up to 4 in parallel)

For a triage with run-meta `(host_name="ssw-giga-02",
host_ip="10.0.x.y", tc="TC-0033-Dram_test_with_exception",
start_ts, end_ts)`:

### 1. `fwlog` (requires IP-style hostname label)

```logql
{job="regression-fwlog", hostname="10.0.x.y", test_name="TC-0033-Dram_test_with_exception"}
```

### 2. `smclog` (requires IP-style hostname label)

```logql
{job="regression-smclog", hostname="10.0.x.y", test_name="TC-0033-Dram_test_with_exception"}
```

### 3. `kernel` (hostname-by-name)

LogQL is read from `[loki].kernel_query_template` (default below) with
`{host}` substituted at runtime:

```logql
{hostname="ssw-giga-02", job=~"varlogs|systemd-journal", filename=~".*kern.*"}
```

### 4. `syslog` (hostname-by-name)

```logql
{hostname="ssw-giga-02", job=~"varlogs|systemd-journal", filename=~".*syslog.*"}
```

The kernel/syslog label schemas are promtail/vector defaults. If the
cluster diverges, the operator overrides via the templates in config.

---

## Label conventions (from `ssw-bundle/test/framework/listeners/loki_publisher.py`)

`fwlog` push (lines 81–86):
```
job        = "regression-fwlog"
hostname   = <host IP, e.g. 10.0.x.y>
device     = <NPU device id, e.g. "rbln0">
test_name  = <Robot Framework test name>
core       = <optional, e.g. "core0">
partition  = <optional, e.g. "p0">
```

`smclog` push (lines 130–135):
```
job        = "regression-smclog"
hostname   = <host IP>
device     = <NPU device id>
test_name  = <Robot Framework test name>
```

**Pivot keys for the bot**:
- `test_name` narrows to the specific TC.
- `hostname` narrows to the host even when multiple hosts run the same TC.
- Both labels are REQUIRED on every query. The wrapper refuses to issue a
  query without them.

---

## Time window

- If `start_ts` and `end_ts` were extracted from the ticket body
  (FR-006), they are the window verbatim.
- If either is missing, the window falls back to
  `created_at ± 30 min` and the handler sets `time_window_fallback=true`
  in the audit row. The persona is told (via the Run Snapshot meta) that
  the window is a fallback.

The window is converted from `datetime` to Loki's nanosecond-precision
unix timestamps:
```python
start_ns = int(start_ts.timestamp() * 1_000_000_000)
end_ns   = int(end_ts.timestamp()   * 1_000_000_000)
```

---

## Per-stream caps

- `limit=5000` line cap (Loki server-side).
- `[loki].per_stream_max_bytes` (default 1 MB) byte cap, enforced
  client-side. If the assembled `LokiSlice.lines` exceeds it, the slice
  is truncated to the most recent fit and `truncated=True` is set. The
  handler tells the persona which streams were truncated.

---

## Hostname↔IP translation

Done by `infra/host_resolver.py:resolve()` once per triage:

```python
host_name = "ssw-giga-02"
host_ip   = socket.gethostbyname(host_name)   # e.g. "10.0.x.y"
```

The result is cached in-process for the duration of the handler call.
If DNS resolution fails:
- `fwlog` and `smclog` queries are SKIPPED (they need IP).
- `kernel` and `syslog` queries continue with `host_name` as-is.
- Audit row records `loki_error="dns_failed:<host_name>"`.

---

## Error contract

- `HTTP 4xx` (other than 429) ⇒ log warning, slice is empty, comment is
  tagged `[loki <stream>: 4xx error]`. Does NOT fail the triage.
- `HTTP 429` ⇒ exponential backoff up to 3 attempts; if all fail, slice
  empty + tagged `[loki <stream>: rate-limited]`.
- `HTTP 5xx` ⇒ retry up to 3 times; if all fail, slice empty + audit
  `loki_error="<stream>:5xx"`.
- Connection refused / timeout ⇒ same as 5xx.

A Loki outage **never** fails the triage outright — kernel/dmesg + RF
artifacts via SSH can carry the load on their own. The audit row makes
the gap visible.

---

## Wrapper API (`infra/loki.py`)

```python
class LokiClient:
    def __init__(self, *, base_url: str, timeout_s: float, http: httpx.AsyncClient): ...

    async def query_range(
        self,
        *,
        hostname: str,                    # REQUIRED — wrapper raises if empty
        start: datetime,                  # UTC
        end: datetime,                    # UTC
        logql_filter: str | None = None,  # additional LogQL clauses, e.g. '{job="regression-fwlog", test_name="..."}'
        limit: int = 5000,
        per_stream_max_bytes: int = 1_048_576,
    ) -> LokiSlice: ...
```

The `logql_filter` is appended to a base selector that always includes
`hostname=<resolved value>`. The wrapper assembles the final LogQL safely
(it does NOT accept raw queries — preventing accidental queries without a
hostname filter, which would scrape unrelated streams).

```python
class LokiQueryBuilder:
    @staticmethod
    def fwlog_for(hostname: str, host_ip: str, tc: str) -> str:
        return f'{{job="regression-fwlog", hostname="{host_ip}", test_name="{_escape(tc)}"}}'

    @staticmethod
    def smclog_for(hostname: str, host_ip: str, tc: str) -> str:
        return f'{{job="regression-smclog", hostname="{host_ip}", test_name="{_escape(tc)}"}}'

    @staticmethod
    def kernel_for(hostname: str, template: str) -> str:
        return template.format(host=hostname)

    @staticmethod
    def syslog_for(hostname: str, template: str) -> str:
        return template.format(host=hostname)
```

The handler calls these builders and passes the result to
`LokiClient.query_range(hostname=..., logql_filter=...)`.

---

## Endpoints we do NOT call

- `POST /loki/api/v1/push` (the bot is read-only; pushing is done by
  ssw-bundle's RF listeners).
- `GET /loki/api/v1/labels`, `/labels/{name}/values` (used for
  discovery in interactive tools; the bot uses fixed label schemas).
- `GET /loki/api/v1/series`, `/tail` (not needed for time-bounded triage).

Any of these would require a spec amendment and a new contract entry.
