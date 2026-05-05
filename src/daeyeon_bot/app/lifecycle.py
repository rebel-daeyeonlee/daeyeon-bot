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
   10. live triggers start     ← `manual` is CLI-only; `gh_review_requested` polls
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
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import aiosqlite
import structlog

from daeyeon_bot.app import heartbeat, pause, ratelimit
from daeyeon_bot.app.config import Config, load
from daeyeon_bot.app.container import Container, ContainerOverrides, build
from daeyeon_bot.app.dispatcher import Dispatcher
from daeyeon_bot.app.lock import AlreadyRunningError, PidLock
from daeyeon_bot.app.registry import TriggerRecord
from daeyeon_bot.core.events import Event
from daeyeon_bot.core.time import Clock
from daeyeon_bot.infra import logging as bot_logging
from daeyeon_bot.infra import outbox, secrets, storage

_log = structlog.get_logger(__name__)

PHASE_B_BUDGET_S = 120.0


@dataclass(slots=True)
class BootOptions:
    config_path: str | None = None
    overrides: ContainerOverrides | None = None
    install_signal_handlers: bool = True
    insecure_env_allowed: bool = False
    # Tests can inject an event that, once set, triggers Phase A like a SIGTERM.
    # When None, boot creates its own internal Event.
    external_stop_event: asyncio.Event | None = None


async def boot(options: BootOptions | None = None) -> None:
    """Boot the daemon. Runs until SIGTERM/SIGINT, returns cleanly afterwards.

    Raises `AlreadyRunningError` if another instance holds the pidfile lock.
    Callers (CLI) should map that to exit code 75 (EX_TEMPFAIL).
    """
    options = options or BootOptions()

    # 1. config; 2. logging
    config = load(options.config_path)
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
            # 4b. apply config-driven rate-limit knobs on top of the seed
            #     row from migration 003. UPSERT preserves `tokens` so a
            #     warm bucket survives restarts.
            await _apply_ratelimit_config(db, config)
            # 5. secrets — fail fast so launchd/systemd surfaces exit 78.
            #    When tests inject a fake claude session factory, skip the
            #    real token probe (no SDK subprocess will spawn).
            oauth_token = _maybe_load_oauth_token(config, options)
            # 7. container, 8. heartbeat, 9. dispatcher, 11. signals
            await _run_supervised(config, db, options, oauth_token=oauth_token)
        finally:
            await _wal_checkpoint(db)
            await db.close()
            _log.info("boot.exit")
    finally:
        pid_lock.close()


async def _apply_ratelimit_config(db: aiosqlite.Connection, config: Config) -> None:
    """UPSERT the `claude_call` bucket with config-driven capacity/refill.

    Preserves `tokens` so a warm bucket survives daemon restarts. The seed
    row comes from migration 003; this just lets operators tune capacity
    and refill via `[ratelimit]` without touching SQL.
    """
    await ratelimit.upsert_bucket(
        db,
        name=ratelimit.CLAUDE_CALL_BUCKET,
        capacity=config.ratelimit.claude_call_capacity,
        refill_per_sec=config.ratelimit.claude_call_refill_per_sec,
        now_iso=datetime.now(tz=UTC).isoformat(),
    )
    await db.commit()


def _maybe_load_oauth_token(config: Config, options: BootOptions) -> str | None:
    """Boot-time secrets probe (PLAN §2.3 step 5). AuthError → exit 78.

    Returns None when a test override already supplies the claude session
    factory — the real CLI subprocess won't spawn so the token is unused.
    """
    if options.overrides is not None and options.overrides.claude_session_factory is not None:
        return None
    provider = secrets.build_provider(
        name=config.secrets.provider,
        keychain_service=config.secrets.keychain_service,
        keychain_account=config.secrets.keychain_account,
        file_path=config.secrets.file_path,
        insecure_env_allowed=options.insecure_env_allowed,
    )
    return provider.load_oauth_token()


async def _run_supervised(
    config: Config,
    db: aiosqlite.Connection,
    options: BootOptions,
    *,
    oauth_token: str | None,
) -> None:
    """Build the container, recover, then run dispatcher + heartbeat under TaskGroup."""
    container = await build(config, db, oauth_token=oauth_token, overrides=options.overrides)
    clock = container.clock
    await _recover_outbox(db, container, clock)

    dispatcher = Dispatcher(
        db=container.db,
        handlers=container.handlers,
        claude_session_factory=container.claude_session_factory,
        clock=clock,
        is_paused=lambda: pause.is_paused(config.pause_flag_path),
    )

    loop = asyncio.get_running_loop()
    stop_event = options.external_stop_event or asyncio.Event()
    cleanup = (
        _install_signal_handlers(loop, stop_event) if options.install_signal_handlers else None
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

    heartbeat_path = _heartbeat_path(config)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(driver())
            tg.create_task(watch_signals())
            tg.create_task(heartbeat.run_until_stopped(heartbeat_path, stop_event))
            for record in container.triggers:
                tg.create_task(_supervised_trigger(record, clock=clock, stop_event=stop_event))
    finally:
        if cleanup is not None:
            cleanup()


@dataclass(slots=True)
class _TriggerCtx:
    """Concrete TriggerContext for the supervised trigger loop."""

    clock: Clock


async def _emit_unused(_event: Event) -> None:
    """Polling triggers persist events directly via SQLite; emit is a no-op."""
    return None


async def _supervised_trigger(
    record: TriggerRecord,
    *,
    clock: Clock,
    stop_event: asyncio.Event,
) -> None:
    """Run a long-running trigger task, halting the daemon on AuthError.

    Unhandled exceptions surface to the TaskGroup so the daemon dies hard
    rather than silently dropping live triggers. The stop event fires so
    the rest of the daemon exits cleanly with the right code. When the
    stop event is set externally, the trigger task is cancelled so the
    TaskGroup can exit during 2-phase shutdown.
    """
    trigger = record.instance
    run = getattr(trigger, "run", None)
    if not callable(run):
        return
    run_coro = cast("Coroutine[Any, Any, None]", run(_emit_unused, _TriggerCtx(clock=clock)))
    run_task: asyncio.Task[None] = asyncio.create_task(run_coro)
    stop_task: asyncio.Task[bool] = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if run_task in done:
            stop_event.set()
            run_task.result()
            return
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
    finally:
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task


async def _recover_outbox(db: aiosqlite.Connection, container: Container, clock: Clock) -> None:
    """Run outbox recovery before dispatcher polls — see PLAN §2.3 step 4."""
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


def _heartbeat_path(config: Config) -> Path:
    return config.state_dir_path / "heartbeat"


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


__all__ = [
    "PHASE_B_BUDGET_S",
    "AlreadyRunningError",
    "BootOptions",
    "Container",
    "boot",
]
