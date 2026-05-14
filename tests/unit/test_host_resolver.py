"""HostResolver — DNS resolution with per-instance caching."""

from __future__ import annotations

import socket
from collections.abc import Callable

import pytest

from daeyeon_bot.infra.host_resolver import HostResolver


def _patch_gethostbyname(monkeypatch: pytest.MonkeyPatch, fn: Callable[[str], str]) -> list[str]:
    """Replace socket.gethostbyname; return the call-log for assertions."""
    log: list[str] = []

    def _wrapped(name: str) -> str:
        log.append(name)
        return fn(name)

    monkeypatch.setattr(socket, "gethostbyname", _wrapped)
    return log


def test_resolve_returns_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gethostbyname(monkeypatch, lambda _n: "10.0.0.5")
    resolver = HostResolver()
    assert resolver.resolve("ssw-giga-02") == "10.0.0.5"


def test_resolve_caches_within_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call for the same name does NOT re-hit DNS."""
    log = _patch_gethostbyname(monkeypatch, lambda _n: "10.0.0.5")
    resolver = HostResolver()
    assert resolver.resolve("ssw-giga-02") == "10.0.0.5"
    assert resolver.resolve("ssw-giga-02") == "10.0.0.5"
    assert log == ["ssw-giga-02"]


def test_resolve_returns_none_on_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(name: str) -> str:
        raise socket.gaierror(-2, "Name or service not known")

    _patch_gethostbyname(monkeypatch, _boom)
    resolver = HostResolver()
    assert resolver.resolve("nonexistent-host") is None


def test_resolve_caches_negative_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed lookup is cached too — we don't retry within one triage."""
    log = _patch_gethostbyname(
        monkeypatch,
        lambda _n: (_ for _ in ()).throw(socket.gaierror(-2, "x")),
    )
    resolver = HostResolver()
    assert resolver.resolve("bad") is None
    assert resolver.resolve("bad") is None
    assert log == ["bad"]


def test_resolve_empty_input_returns_none_without_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _patch_gethostbyname(monkeypatch, lambda _n: "10.0.0.5")
    resolver = HostResolver()
    assert resolver.resolve("") is None
    assert resolver.resolve("   ") is None
    assert log == []


def test_distinct_names_cached_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"a": "1.1.1.1", "b": "2.2.2.2"}
    log = _patch_gethostbyname(monkeypatch, lambda n: answers[n])
    resolver = HostResolver()
    assert resolver.resolve("a") == "1.1.1.1"
    assert resolver.resolve("b") == "2.2.2.2"
    assert resolver.resolve("a") == "1.1.1.1"
    assert log == ["a", "b"]


def test_resolve_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_gethostbyname(monkeypatch, lambda _n: "10.0.0.5")
    resolver = HostResolver()
    assert resolver.resolve("  ssw-giga-02  ") == "10.0.0.5"
    assert log == ["ssw-giga-02"]
