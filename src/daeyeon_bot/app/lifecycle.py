"""Boot and shutdown orchestration.

Boot order (DO NOT REORDER — see `docs/PLAN.md` §2.3):
    1. config load
    2. logging init
    3. pidfile + flock        ← Phase 2
    4. SQLite open + migrate
    5. secrets load            ← Phase 4
    6. permission probe        ← Phase 4
    7. container build
    8. heartbeat task          ← Phase 3
    9. dispatcher start
   10. triggers start          ← Phase 1: manual is fired via CLI; no live triggers
   11. wait for SIGTERM / SIGINT

Shutdown is 2-phase with a 180s budget (PLAN.md §2.4):
    Phase A — stop accepting new work (instant)
    Phase B — drain in-flight handlers (up to PHASE_B_BUDGET_S)
    Phase C — close resources, WAL checkpoint, release lock (best-effort, no
              hard cap; supervisor SIGKILLs after the outer 180s anyway)
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable
from dataclasses import dataclass

import aiosqlite
import structlog

from daeyeon_bot.app.config import load
from daeyeon_bot.app.container import Container, ContainerOverrides, build
from daeyeon_bot.app.dispatcher import Dispatcher
from daeyeon_bot.app.lock import AlreadyRunningError, PidLock
from daeyeon_bot.core.time import Clock
from daeyeon_bot.infra import logging as bot_logging
from daeyeon_bot.infra import outbox, storage

_log = structlog.get_logger(__name__)

PHASE_B_BUDGET_S = 120.0


@dataclass(slots=True)
class BootOptions:
    config_path: str | None = None
    overrides: ContainerOverrides | None = None
    install_signal_handlers: bool = True
    # Tests can inject an event that, once set, triggers Phase A like a SIGTERM.
    # When None, boot creates its own internal Event.
    external_stop_event: asyncio.Event | None = None


async def boot(options: BootOptions | None = None) -> None:
    """Boot the daemon. Runs until SIGTERM/SIGINT, returns cleanly afterwards.

    Raises `AlreadyRunningError` if another instance holds the pidfile lock.
    Callers (CLI) should map that to exit code 75 (EX_TEMPFAIL).
    """
    options = options or BootOptions()

    # 1. config
    config = load(options.config_path)
    # 2. logging
    bot_logging.init(level=config.logging.level, fmt=config.logging.format)
    _log.info("boot.start", state_dir=str(config.state_dir_path))

    # 3. pidfile + flock
    config.state_dir_path.mkdir(parents=True, exist_ok=True)
    pid_lock = PidLock(path=config.pidfile_path)
    pid_lock.acquire()
    try:
        # 4. storage
        db = await storage.open_db(config.db_path)
        try:
            await storage.apply_migrations(db)

            # 5. secrets — Phase 4.
            # 6. permissions — Phase 4.
            # 7. container
            container = build(config, db, overrides=options.overrides)
            # 8. heartbeat — Phase 3.

            # Recover any rows that the previous process left in 'running' or
            # 'interrupted' state. Must run before the dispatcher starts polling.
            clock = container.clock
            registry = container.handlers
            idempotent = {name for name, rec in registry.by_name.items() if rec.manifest.idempotent}
            known = set(registry.by_name)
            report = await outbox.recover_interrupted_rows(
                db,
                idempotent_handlers=idempotent,
                known_handlers=known,
                now=clock.now(),
            )
            if report.crashed or report.rerun or report.dead_lettered:
                _log.info(
                    "boot.recovery",
                    crashed=report.crashed,
                    rerun=report.rerun,
                    dead_lettered=report.dead_lettered,
                )

            # 9. dispatcher
            dispatcher = Dispatcher(
                db=container.db,
                handlers=container.handlers,
                claude_session_factory=container.claude_session_factory,
                clock=clock,
            )

            # 11. signal-driven shutdown
            loop = asyncio.get_running_loop()
            stop_event = options.external_stop_event or asyncio.Event()
            cleanup = (
                _install_signal_handlers(loop, stop_event)
                if options.install_signal_handlers
                else None
            )

            async def watch_signals() -> None:
                await stop_event.wait()
                _log.info("shutdown.phase_a", reason="signal")
                dispatcher.request_stop_claiming()

            async def driver() -> None:
                try:
                    await _drive_dispatcher(dispatcher, db, clock)
                finally:
                    # Wake watch_signals if the dispatcher self-stopped
                    # (e.g., AuthError) so the TaskGroup can exit cleanly.
                    stop_event.set()

            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(driver())
                    tg.create_task(watch_signals())
            finally:
                if cleanup is not None:
                    cleanup()
        finally:
            await _wal_checkpoint(db)
            await db.close()
            _log.info("boot.exit")
    finally:
        pid_lock.close()


async def _drive_dispatcher(dispatcher: Dispatcher, db: aiosqlite.Connection, clock: Clock) -> None:
    """Run the dispatcher poll loop, then drain in-flight on stop."""
    await dispatcher.run()
    _log.info("shutdown.phase_b", budget_s=PHASE_B_BUDGET_S)
    timed_out = await dispatcher.drain(budget_s=PHASE_B_BUDGET_S)
    if timed_out:
        marked = await outbox.mark_interrupted(
            db,
            outbox_ids=timed_out,
            now=clock.now(),
            reason="shutdown drain timeout",
        )
        _log.warning("shutdown.drain_timeout", timed_out=len(timed_out), interrupted=marked)


async def _wal_checkpoint(db: aiosqlite.Connection) -> None:
    """Phase C: best-effort WAL truncation so the next boot starts clean."""
    try:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        await db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        _log.warning("shutdown.wal_checkpoint_failed", error=str(exc))


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event
) -> Callable[[], None]:
    """Wire SIGTERM/SIGINT to set `stop_event`. Returns a cleanup callable."""
    handled: list[int] = []

    def _set() -> None:
        if not stop_event.is_set():
            stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _set)
            handled.append(sig)
        except NotImplementedError:  # pragma: no cover — Windows / restricted envs
            continue

    def _cleanup() -> None:
        for sig in handled:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)

    return _cleanup


async def shutdown() -> None:
    """Module-level helper kept for symmetry; real shutdown is driven by `boot`."""
    return None


__all__ = [
    "PHASE_B_BUDGET_S",
    "AlreadyRunningError",
    "BootOptions",
    "Container",
    "boot",
    "shutdown",
]
