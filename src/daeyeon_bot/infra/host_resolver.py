"""Hostname → IP resolver with per-instance caching.

The `jira_triage` handler needs to translate SSW test-host names
(`ssw-giga-02`) to IPs for Loki fwlog/smclog queries, whose `hostname`
label is the IP (see `loki_publisher.py:83`). kernel/syslog queries use
the hostname-by-name as-is.

One resolver instance is created per triage and discarded after. The
in-process cache survives a single handler call (so the kernel + fwlog
query passes share the lookup) but not across triages — if a host is
re-imaged between triages, the next one re-resolves.

DNS failures are non-fatal: `resolve(name)` returns `None`, and the
caller is expected to skip the IP-dependent stream rather than fail the
whole triage. See FR-013 / `contracts/loki-query-surface.md`.
"""

from __future__ import annotations

import socket
import threading


class HostResolver:
    """`socket.gethostbyname` with a per-instance dict cache."""

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def resolve(self, name: str) -> str | None:
        """Return the IPv4 string for `name`, or None on DNS failure.

        Empty/whitespace input returns None without touching DNS.
        """
        key = (name or "").strip()
        if not key:
            return None
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        try:
            ip = socket.gethostbyname(key)
        except OSError:
            # gaierror / herror / etc. — all map to "DNS failed".
            ip = None
        with self._lock:
            self._cache[key] = ip
        return ip


__all__ = ["HostResolver"]
