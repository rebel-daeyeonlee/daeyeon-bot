"""Heartbeat loop: touch a file every `tick_s` seconds, optionally sd_notify.

PLAN.md §2.3 step 8. The lifecycle starts `run_until_stopped()` as a TaskGroup
member and stops it via the same stop_event used for shutdown.

A stale heartbeat (mtime older than ~3x tick) is the signal supervisors and
`ops doctor` use to detect a hung daemon. We deliberately do not log on every
tick to keep the structlog stream useful — but if a tick wakes up much later
than scheduled (event loop blocked, system paused, GC stall, …) we *do* log
an error so journald / launchd-stderr surfaces the regression in real time.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

DEFAULT_TICK_S = 30.0
STALE_FACTOR = 3  # heartbeat older than tick * factor → treated as stale by doctor


async def run_until_stopped(
    flag_path: Path,
    stop_event: asyncio.Event,
    *,
    tick_s: float = DEFAULT_TICK_S,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Touch `flag_path` every `tick_s` until `stop_event` is set.

    If a tick wakes up later than `tick_s * STALE_FACTOR`, log an error
    once for that tick — the daemon is otherwise blind to a self-stall.
    """
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    _touch(flag_path)
    _sd_notify_ready()
    stale_threshold_s = tick_s * STALE_FACTOR
    last_tick_ts = clock()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_s)
        except TimeoutError:
            now_ts = clock()
            elapsed_s = now_ts - last_tick_ts
            if elapsed_s > stale_threshold_s:
                _log.error(
                    "heartbeat.tick_lag",
                    elapsed_s=round(elapsed_s, 3),
                    tick_s=tick_s,
                    threshold_s=stale_threshold_s,
                )
            _touch(flag_path)
            _sd_notify_watchdog()
            last_tick_ts = now_ts


def staleness_seconds(flag_path: Path, *, now_ts: float) -> float | None:
    """Seconds since the heartbeat was last touched, or None if missing."""
    try:
        mtime = flag_path.stat().st_mtime
    except FileNotFoundError:
        return None
    return now_ts - mtime


def is_stale(flag_path: Path, *, now_ts: float, tick_s: float = DEFAULT_TICK_S) -> bool:
    """True iff the heartbeat file is missing or older than tick_s * STALE_FACTOR."""
    age = staleness_seconds(flag_path, now_ts=now_ts)
    if age is None:
        return True
    return age > tick_s * STALE_FACTOR


def _touch(flag_path: Path) -> None:
    flag_path.touch(mode=0o600)


def _sd_notify_ready() -> None:
    if _send_sd_notify("READY=1"):
        _log.info("heartbeat.sd_notify_ready", socket=os.environ.get("NOTIFY_SOCKET"))


def _sd_notify_watchdog() -> None:
    _send_sd_notify("WATCHDOG=1")


def _send_sd_notify(message: str) -> bool:
    """Best-effort sd_notify. Returns True if the datagram was sent.

    No-op (returns False) outside systemd's notify socket. We avoid pulling
    in `systemd` libs; the protocol is a one-line UDP send.
    """
    socket_path = os.environ.get("NOTIFY_SOCKET")
    if not socket_path:
        return False
    try:
        # Abstract namespace sockets begin with NUL on Linux.
        addr = "\0" + socket_path[1:] if socket_path.startswith("@") else socket_path
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), addr)
        return True
    except OSError as exc:
        # Promoted from debug to warning: when Type=notify is in play, a
        # silent sendto failure leaves the unit stuck in `activating (start)`
        # until TimeoutStartSec. Surfacing the OSError tells the operator
        # whether it's EACCES (perms / namespace) vs ENOENT (path missing).
        _log.warning(
            "heartbeat.sd_notify_failed",
            error=str(exc),
            message=message,
            socket=socket_path,
        )
        return False


__all__ = [
    "DEFAULT_TICK_S",
    "STALE_FACTOR",
    "is_stale",
    "run_until_stopped",
    "staleness_seconds",
]
