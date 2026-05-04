"""Outbox poll loop with 2-phase shutdown semantics.

Lifecycle:
    run()            — blocks; claims rows and spawns _run_one tasks until either
                       request_stop_claiming() or stop() fires.
    request_stop_claiming() — Phase A: stop accepting new work. run() returns.
    drain(timeout)   — Phase B: wait for in-flight tasks to finish. Returns the
                       outbox_ids that timed out so the caller can mark them
                       'interrupted'.
    stop()           — Hard stop: set both flags. Used by the AuthError branch
                       and tests that want immediate halt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast

import aiosqlite
import structlog

from daeyeon_bot.app.registry import HandlerRecord, HandlerRegistry
from daeyeon_bot.core.errors import (
    AuthError,
    PermanentError,
    QuotaError,
    RateLimitError,
    TransientError,
)
from daeyeon_bot.core.events import Event
from daeyeon_bot.core.results import Ack, DeadLetter, HandlerResult, Retry
from daeyeon_bot.core.time import Clock, SystemClock
from daeyeon_bot.infra import outbox

_log = structlog.get_logger(__name__)

DEFAULT_BACKOFF_S = 30.0
RATE_LIMIT_BACKOFF_S = 60.0
PAUSE_BACKOFF_S = 5.0
# Cap on transient retries per outbox row. After this many attempts the
# dispatcher gives up and dead-letters instead of looping forever — empirically
# we hit attempt=142 on a stuck PR before adding this. Operators recover with
# `ops replay --confirm` once the underlying issue is addressed.
MAX_TRANSIENT_ATTEMPTS = 10


@dataclass(slots=True)
class _HandlerCtx:
    """Concrete HandlerContext built per dispatch."""

    clock: Clock
    trace_id: str
    claude_session_factory: object


@dataclass(slots=True)
class Dispatcher:
    """Polls outbox and dispatches claimed rows to handlers."""

    db: aiosqlite.Connection
    handlers: HandlerRegistry
    claude_session_factory: object
    clock: Clock = field(default_factory=SystemClock)
    poll_interval_s: float = 0.5
    claim_id: str = "dispatcher-1"
    # Returns True when the operator wants new dispatches blocked (PAUSE flag).
    # In-flight handlers keep running; the loop just stops claiming.
    is_paused: Callable[[], bool] = field(default=lambda: False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _stop_claim: asyncio.Event = field(default_factory=asyncio.Event)
    _semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict[str, asyncio.Semaphore])
    # outbox_id → in-flight asyncio.Task. Used for drain-with-timeout.
    _inflight: dict[int, asyncio.Task[None]] = field(default_factory=dict[int, asyncio.Task[None]])

    async def run(self) -> None:
        """Poll until `request_stop_claiming()` or `stop()`. Returns immediately
        once Phase A is requested — the caller is responsible for `drain()`."""
        while not self._is_done():
            if self.is_paused():
                await self._wait_for_stop_or_tick()
                continue
            job = await outbox.claim_one(self.db, claimed_by=self.claim_id, now=self.clock.now())
            if job is None:
                await self._wait_for_stop_or_tick()
                continue

            handler_record = self.handlers.by_name.get(job.handler)
            if handler_record is None:
                _log.warning(
                    "dispatcher.handler_missing",
                    outbox_id=job.outbox_id,
                    handler=job.handler,
                )
                started = self.clock.now()
                await outbox.settle(
                    self.db,
                    job=job,
                    result=DeadLetter(reason=f"handler not registered: {job.handler}"),
                    started_at=started,
                    finished_at=started,
                    dedup_ttl=None,
                )
                continue

            # Idempotent handlers: skip if a non-expired dedup_keys row exists.
            # This is defense-in-depth against re-queued rows that share the
            # same (event_id, handler, attempt_epoch) as a prior Ack.
            if handler_record.manifest.idempotent and await outbox.is_deduped(
                self.db,
                event_id=job.event.id,
                handler=job.handler,
                attempt_epoch=job.attempt_epoch,
                now=self.clock.now(),
            ):
                started = self.clock.now()
                _log.info(
                    "dispatcher.deduped",
                    outbox_id=job.outbox_id,
                    handler=job.handler,
                    event_id=job.event.id,
                )
                await outbox.settle(
                    self.db,
                    job=job,
                    result=Ack(),
                    started_at=started,
                    finished_at=started,
                    dedup_ttl=handler_record.manifest.dedup_ttl,
                )
                continue

            semaphore = self._semaphores.setdefault(
                job.handler, asyncio.Semaphore(handler_record.manifest.concurrency)
            )
            task = asyncio.create_task(self._run_one(job, handler_record, semaphore))
            self._inflight[job.outbox_id] = task
            task.add_done_callback(lambda t, oid=job.outbox_id: self._inflight.pop(oid, None))

    def request_stop_claiming(self) -> None:
        """Phase A of shutdown: stop pulling new rows. In-flight keeps running."""
        self._stop_claim.set()

    def stop(self) -> None:
        """Hard stop. Used by AuthError; tests may use it to halt immediately."""
        self._stop.set()
        self._stop_claim.set()

    async def drain(self, budget_s: float) -> list[int]:
        """Phase B: wait up to `budget_s` seconds for in-flight handlers.

        Returns the list of outbox_ids that did not finish in time. Those tasks
        get cancelled; the caller should mark their rows 'interrupted' so the
        next boot's recovery decides retry vs DLQ.
        """
        if not self._inflight:
            return []
        tasks = list(self._inflight.values())
        _, pending = await asyncio.wait(tasks, timeout=budget_s)
        if not pending:
            return []
        timed_out = [oid for oid, t in self._inflight.items() if t in pending]
        for task in pending:
            task.cancel()
        # Brief settle so cancellations propagate before lifecycle closes the DB.
        await asyncio.gather(*pending, return_exceptions=True)
        return timed_out

    def _is_done(self) -> bool:
        return self._stop.is_set() or self._stop_claim.is_set()

    async def _wait_for_stop_or_tick(self) -> None:
        try:
            await asyncio.wait_for(
                asyncio.wait(
                    [
                        asyncio.ensure_future(self._stop.wait()),
                        asyncio.ensure_future(self._stop_claim.wait()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                ),
                timeout=self.poll_interval_s,
            )
        except TimeoutError:
            pass

    async def _run_one(
        self, job: outbox.ClaimedJob, record: HandlerRecord, semaphore: asyncio.Semaphore
    ) -> None:
        async with semaphore:
            started = self.clock.now()
            result: HandlerResult
            try:
                raw_result = await record.instance.handle(  # type: ignore[attr-defined]
                    job.event,
                    _HandlerCtx(
                        clock=self.clock,
                        trace_id=job.event.trace_id,
                        claude_session_factory=self.claude_session_factory,
                    ),
                )
                result = cast("HandlerResult", raw_result)
            except AuthError:
                # Auth errors halt the daemon. The current row stays 'running'
                # until the next boot's recover_interrupted_rows handles it.
                # Lifecycle reports exit 78 (Phase 4 wires the exit code).
                _log.error("dispatcher.auth_error", outbox_id=job.outbox_id)
                self.stop()
                return
            except RateLimitError as exc:
                result = self._classify_transient(exc, RATE_LIMIT_BACKOFF_S, job)
            except QuotaError as exc:
                # PAUSE flag or local rate-limit token bucket — short backoff
                # so the row resumes promptly once the operator clears PAUSE.
                result = self._classify_transient(exc, PAUSE_BACKOFF_S, job)
            except TransientError as exc:
                result = self._classify_transient(exc, DEFAULT_BACKOFF_S, job)
            except (PermanentError, Exception) as exc:
                result = DeadLetter(reason=f"{type(exc).__name__}: {exc}")
            finished = self.clock.now()

            dedup_ttl: timedelta | None = None
            if isinstance(result, Ack) and record.manifest.idempotent:
                dedup_ttl = record.manifest.dedup_ttl

            await outbox.settle(
                self.db,
                job=job,
                result=result,
                started_at=started,
                finished_at=finished,
                dedup_ttl=dedup_ttl,
            )
            _log.info(
                "dispatcher.settled",
                outbox_id=job.outbox_id,
                handler=job.handler,
                event_id=job.event.id,
                status=_result_to_status(result),
            )

    @staticmethod
    def _classify_transient(
        exc: Exception, default_backoff: float, job: outbox.ClaimedJob
    ) -> Retry | DeadLetter:
        # Future: read RateLimitError.retry_after if the SDK exposes it.
        # Log here so operators can diagnose retries from journald — Retry results
        # don't carry the exception, and outbox.settle clears last_error to NULL.
        next_attempt = job.attempt + 1
        if next_attempt >= MAX_TRANSIENT_ATTEMPTS:
            _log.warning(
                "dispatcher.handler_transient_exhausted",
                outbox_id=job.outbox_id,
                handler=job.handler,
                event_id=job.event.id,
                attempt=next_attempt,
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            return DeadLetter(
                reason=(
                    f"max transient attempts ({MAX_TRANSIENT_ATTEMPTS}) reached: "
                    f"{type(exc).__name__}: {exc}"
                )
            )
        _log.warning(
            "dispatcher.handler_transient",
            outbox_id=job.outbox_id,
            handler=job.handler,
            event_id=job.event.id,
            attempt=next_attempt,
            exc_type=type(exc).__name__,
            error=str(exc),
            backoff_s=default_backoff,
        )
        return Retry(after_s=default_backoff)


def _result_to_status(result: HandlerResult) -> str:
    match result:
        case Ack():
            return "acked"
        case Retry():
            return "retry"
        case DeadLetter():
            return "dead_letter"


# Re-exported types so callers don't reach into infra.outbox.
__all__ = [
    "DEFAULT_BACKOFF_S",
    "MAX_TRANSIENT_ATTEMPTS",
    "RATE_LIMIT_BACKOFF_S",
    "Dispatcher",
    "Event",
]
