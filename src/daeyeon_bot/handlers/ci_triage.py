"""`ci_triage` handler — first-pass CI-failure triage (feature 003 P1).

Accepts `ci.triage.manual` (CLI fire) and `slack.ci_alert` (P2 polling trigger).
Pipeline: parse alert → extract run → `gh --log-failed` → ANSI-strip + redact +
error-anchored truncate → OnCall-wiki signature search → Claude SDK → render a
header-first Slack summary → post (dry_run channel in P1; thread reply in P3) →
audit. Read-only everywhere except the single `chat.postMessage`.

The handler returns `Ack`/`Retry`/`DeadLetter` or raises typed `core.errors`;
the dispatcher centralizes exception→result mapping (it counts retries — the
handler has no attempt counter). See specs/003-ci-monitor-bot/plan.md §Pipeline.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import structlog
from pydantic import ValidationError as PydanticValidationError

from daeyeon_bot.app.config import CiTriageHandlerEntry
from daeyeon_bot.core.ci_triage.types import (
    AuditRow,
    LokiWindow,
    ParsedAlert,
    RunRef,
    WikiMatch,
)
from daeyeon_bot.core.errors import PermanentError, RunLogUnavailableError, TransientError
from daeyeon_bot.core.events import Event
from daeyeon_bot.core.manifest import HandlerManifest
from daeyeon_bot.core.persona import Persona
from daeyeon_bot.core.protocols import HandlerContext
from daeyeon_bot.core.results import Ack, DeadLetter, HandlerResult
from daeyeon_bot.handlers.ci_triage_cross_run import CrossRunResult, analyze_cross_run
from daeyeon_bot.handlers.ci_triage_parsing import error_anchored_windows
from daeyeon_bot.handlers.ci_triage_schemas import TriageOutput, enforce_confidence_floor
from daeyeon_bot.infra import alert_parse, dmesg_timeline
from daeyeon_bot.infra.ci_triage_audit import (
    count_recent_by_signature,
    find_posted_for_message,
    find_posted_for_run,
    insert_audit,
)
from daeyeon_bot.infra.logging import redact_text, redact_with_provenance
from daeyeon_bot.infra.oncall_wiki import WikiRefresh
from daeyeon_bot.infra.persona_loader import PersonaLoader
from daeyeon_bot.infra.slack import PostResult

_log = structlog.get_logger(__name__)

MANIFEST = HandlerManifest(
    name="ci_triage",
    idempotent=True,
    dedup_ttl=timedelta(days=1),
    side_effect_key=None,
    # concurrency=1 is load-bearing: the secondary (repo, run_id) duplicate guard
    # is an audit LOOKUP (not a SQL UNIQUE), correct ONLY because concurrency=1
    # serializes claims so the second event sees the first's 'posted' row. Do NOT
    # raise this without converting the guard to a real constraint / claimed-row lock.
    concurrency=1,
    accepts=("slack.ci_alert", "ci.triage.manual"),
)

PauseGuard = Callable[[], Awaitable[None]]


async def _no_pause() -> None:
    return None


@runtime_checkable
class _SlackClient(Protocol):
    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = ...,
        username: str | None = ...,
        icon_emoji: str | None = ...,
    ) -> PostResult: ...


@runtime_checkable
class _GhClient(Protocol):
    async def run_failed_job_logs(self, repo: str, run_id: str) -> str: ...
    async def run_failed_annotations(self, repo: str, run_id: str) -> str: ...
    async def failed_jobs(self, repo: str, run_id: str) -> list[Any]: ...
    async def run_meta(self, repo: str, run_id: str) -> Any: ...
    async def list_workflow_runs(
        self, repo: str, *, workflow_id: str, per_page: int = ..., branch: str | None = ...
    ) -> list[Any]: ...


@runtime_checkable
class _OncallWiki(Protocol):
    async def ensure_fresh(self) -> WikiRefresh: ...
    async def search(
        self, *, signatures: tuple[str, ...], phrases: tuple[str, ...]
    ) -> list[WikiMatch]: ...


@dataclass(slots=True)
class CiTriageHandler:
    """Consumes `ci.triage.manual` and `slack.ci_alert` events."""

    manifest: HandlerManifest
    slack: _SlackClient
    gh: _GhClient
    oncall_wiki: _OncallWiki
    persona_loader: PersonaLoader
    config: CiTriageHandlerEntry
    db: Any  # aiosqlite.Connection
    loki: Any = None  # LokiClient | FakeLoki | None — device-level dual-evidence path
    jira: Any = None  # JiraClient | None — P2/P4 ticket search (best-effort)
    linear: Any = None  # LinearClient | None — P2/P4 ticket search (best-effort)
    pause_guard: PauseGuard = _no_pause

    async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
        budget = float(self.config.timeout_seconds)
        try:
            return await asyncio.wait_for(self._handle_inner(event, ctx), timeout=budget)
        except TimeoutError as exc:
            raise TransientError(f"ci_triage exceeded {budget}s budget (asyncio.wait_for)") from exc

    async def _handle_inner(  # noqa: PLR0912, PLR0915 — multi-stage pipeline; each branch documented
        self, event: Event, ctx: HandlerContext
    ) -> HandlerResult:
        await self.pause_guard()
        now = ctx.clock.now() if hasattr(ctx, "clock") else datetime.now(tz=UTC)
        alert, force = _parse_event(event)

        # P3: with no run link / Loki window, a pasted failure log (fenced ``` +
        # error signature) is still triageable evidence (degraded — no gh fetch).
        pasted_log = ""
        if (
            alert.run_ref is None
            and alert.loki_window is None
            and self.config.log_only_triage_enabled
        ):
            block = alert_parse.extract_log_block(alert.merged_text)
            if alert_parse.has_error_signature(block):
                pasted_log = block

        # Gate: nothing machine-readable AND no pasted log → nothing to triage.
        if alert.run_ref is None and alert.loki_window is None and not pasted_log:
            await self._maybe_post_no_evidence_note(
                alert, reason="machine-readable run/Loki 링크를 못 찾았습니다"
            )
            await self._audit(event, alert, status="skipped_no_run_link", now=now)
            return Ack()

        # Idempotency guards (concurrency=1 serializes; see manifest note).
        if not force:
            if (
                await find_posted_for_message(
                    self.db, channel_id=alert.channel_id, message_ts=alert.message_ts
                )
                is not None
            ):
                await self._audit(event, alert, status="skipped_already_triaged", now=now)
                return Ack()
            if (
                alert.run_ref is not None
                and await find_posted_for_run(
                    self.db, repo=alert.run_ref.repo, run_id=alert.run_ref.run_id
                )
                is not None
            ):
                await self._audit(event, alert, status="skipped_already_triaged", now=now)
                return Ack()

        persona = self.persona_loader.load(
            self.config.persona_skill or "daeyeon-bot-ci-triage",
            min_chars=self.config.min_persona_chars,
        )

        # ── EVIDENCE: run-log path (primary) + check-run annotations ──────────
        log_text = ""
        annotations = ""
        gh_error: str | None = None
        if alert.run_ref is not None:
            # Annotations carry the concise failure reason even when a job's log
            # blob is gone (e.g. a self-hosted runner that lost communication
            # mid-step never uploads its log). Best-effort.
            try:
                annotations = redact_text(
                    await self.gh.run_failed_annotations(alert.run_ref.repo, alert.run_ref.run_id)
                )
            except Exception as exc:
                _log.info("ci_triage.annotations_failed", error=repr(exc))
            try:
                raw = await self.gh.run_failed_job_logs(alert.run_ref.repo, alert.run_ref.run_id)
            except RunLogUnavailableError as exc:
                gh_error = f"log_unavailable:{str(exc)[:80]}"
                # Skip ONLY when no other evidence exists. Annotations (runner-death
                # reason) or a Loki window can still ground a triage.
                if not annotations and alert.loki_window is None:
                    await self._maybe_post_no_evidence_note(
                        alert, reason="GitHub run 로그가 만료/삭제됐습니다"
                    )
                    await self._audit(
                        event, alert, status="skipped_log_unavailable", now=now, gh_error=gh_error
                    )
                    return Ack()
            else:
                # strip ANSI + redact BEFORE anchoring / logging / prompting.
                anchored = error_anchored_windows(redact_text(raw))
                log_text = anchored
                _log.info(
                    "ci_triage.log_collected",
                    repo=alert.run_ref.repo,
                    run_id=alert.run_ref.run_id,
                    raw_chars=len(raw),
                    anchored_chars=len(anchored),
                )

        # P3 degraded path: no run log, but a pasted failure log is the evidence.
        if not log_text and pasted_log:
            log_text = error_anchored_windows(redact_text(pasted_log))
            _log.info("ci_triage.log_only", anchored_chars=len(log_text))

        # ── EVIDENCE: DUT/host Loki (3-tier host resolution) ──────────────────
        # Prefer the device-under-test host over the runner: a premerge job runs on
        # a controller runner (ssw-hp-01) but tests a separate DUT (ssw-host-04),
        # where the device errors actually are. Order: (1) alert [host] tag
        # (dev_syssw_test gives the DUT) → (2) DUT host grepped from the failed-job
        # log → (3) runner_name fallback (atom-test: runner == DUT).
        loki_text = ""
        loki_window = alert.loki_window
        if loki_window is None and alert.run_ref is not None:
            loki_window = await self._resolve_dut_window(alert.run_ref, log_text, now=now)
        elif loki_window is None and pasted_log:
            # log-only: grep a DUT host straight from the pasted log.
            hosts = alert_parse.extract_dut_hosts(pasted_log)
            if hosts:
                loki_window = LokiWindow(host=hosts[0], start=None, end=None)
        if loki_window is not None:
            loki_text, loki_err = await self._loki_evidence(loki_window, now=now)
            if loki_err:
                _log.info("ci_triage.loki_degraded", host=loki_window.host, error=loki_err)

        # ── EVIDENCE: cross-run comparison (P1) — "다른 PR도 fail?" ─────────────
        cross_run: CrossRunResult | None = None
        if self.config.cross_run_enabled and alert.run_ref is not None:
            cross_run = await self._cross_run_evidence(alert.run_ref)

        # ── EVIDENCE: ssw-debugger dmesg-timeline domain tagging (best-effort) ─
        timeline_text = ""
        if loki_text and self.config.dmesg_timeline_script:
            summary = await dmesg_timeline.classify(
                loki_text, script_path=self.config.dmesg_timeline_script
            )
            if summary is not None:
                timeline_text = redact_text(summary.as_prompt())
                _log.info("ci_triage.dmesg_timeline", by_domain=summary.by_domain)

        # ── OnCall wiki ───────────────────────────────────────────────────────
        wiki_error: str | None = None
        wiki_matches: list[WikiMatch] = []
        try:
            refresh: WikiRefresh = await self.oncall_wiki.ensure_fresh()
            if refresh.error:
                wiki_error = refresh.error
            if refresh.available:
                signatures, phrases = _search_terms(alert, f"{annotations}\n{log_text}")
                wiki_matches = await self.oncall_wiki.search(signatures=signatures, phrases=phrases)
        except Exception as exc:
            wiki_error = f"wiki_failed:{str(exc)[:80]}"

        has_wiki = bool(wiki_matches)
        has_strong_anchor = any(m.signature_matched for m in wiki_matches)

        # ── Claude ────────────────────────────────────────────────────────────
        triage = await self._call_claude_with_retry(
            ctx=ctx,
            persona=persona,
            alert=alert,
            log_text=log_text,
            annotations=annotations,
            loki_text=loki_text,
            wiki_matches=wiki_matches,
            cross_run=cross_run,
            timeline_text=timeline_text,
        )
        triage = enforce_confidence_floor(
            triage,
            has_strong_anchor=has_strong_anchor,
            has_wiki_match=has_wiki,
            has_cross_run_signal=bool(cross_run and cross_run.signal),
        )

        # ── P2: recurrence (audit DB) + ticket search (Jira/Linear) ───────────
        dut_host = loki_window.host if loki_window is not None else None
        signature = _failure_signature(triage.headline, triage.classification)
        recurrence_line = await self._recurrence_line(
            signature=signature, message_ts=alert.message_ts, now=now
        )
        tickets = await self._ticket_search(dut_host=dut_host, triage=triage)
        ticket_draft = _ticket_draft(triage, tickets, enabled=self.config.ticket_draft_enabled)

        # ── Render (+ force-supersede header) + redaction guard ───────────────
        body = _render_slack_body(
            alert,
            triage,
            wiki_matches,
            cross_run=cross_run.summary_ko() if cross_run else None,
            recurrence=recurrence_line,
            tickets=tickets,
            ticket_draft=ticket_draft,
        )
        if force:
            prior = await self._prior_posted(alert)
            if prior is not None:
                body = _supersede_header(prior) + body
        _, spans = redact_with_provenance(body)
        if spans:
            await self._audit(
                event, alert, status="failed", now=now, error="redaction would alter posted content"
            )
            return DeadLetter("ci_triage: redaction would alter posted Slack body")

        # ── POST (single write) ───────────────────────────────────────────────
        target_channel, thread_ts = self._post_target(alert)
        result = await self.slack.post_message(
            target_channel,
            body,
            thread_ts=thread_ts,
            username="CI Triage",
            icon_emoji=":robot_face:",
        )

        await self._audit(
            event,
            alert,
            status="posted",
            now=now,
            attribution=triage.attribution,
            classification=triage.classification,
            owner_area=triage.owner_area,
            confidence=triage.confidence,
            wiki_matches=tuple(m.path for m in wiki_matches),
            posted_channel_id=result.channel,
            posted_message_ts=result.ts,
            summary_chars=len(body),
            persona_skill=persona.name,
            persona_mtime_ns=persona.mtime_ns,
            gh_error=gh_error,
            wiki_error=wiki_error,
            dut_host=dut_host,
            signature=signature,
        )
        _log.info(
            "ci_triage.posted",
            channel=result.channel,
            ts=result.ts,
            attribution=triage.attribution,
            confidence=triage.confidence,
        )
        return Ack()

    # ── Claude ─────────────────────────────────────────────────────────────────

    async def _call_claude_with_retry(
        self,
        *,
        ctx: HandlerContext,
        persona: Persona,
        alert: ParsedAlert,
        log_text: str,
        annotations: str,
        loki_text: str,
        wiki_matches: list[WikiMatch],
        cross_run: CrossRunResult | None = None,
        timeline_text: str = "",
    ) -> TriageOutput:
        """Two in-call attempts. Second parse/validate failure → DeadLetter
        (PermanentError). This is an in-call loop, NOT a dispatcher retry."""
        system_prompt = persona.body + _SCHEMA_APPENDIX
        user_message = _render_user_message(
            alert, log_text, annotations, loki_text, wiki_matches, cross_run, timeline_text
        )
        last_error: str | None = None
        for attempt in range(2):
            session = ctx.claude_session_factory()
            prompt = user_message
            if last_error is not None:
                prompt = (
                    f"{user_message}\n\n---\nYour previous response failed validation:\n"
                    f"{last_error}\nFix and return ONLY a valid JSON object."
                )
            async with session as s:  # type: ignore[attr-defined]
                text_obj: object = await s.query(prompt, system=system_prompt)  # type: ignore[attr-defined]
            text = text_obj if isinstance(text_obj, str) else str(text_obj)  # type: ignore[arg-type]
            try:
                data = json.loads(_strip_code_fence(text))
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = f"JSON parse error: {exc}"
                if attempt + 1 < 2:
                    continue
                raise PermanentError(f"ci_triage: malformed JSON after retry: {exc}") from exc
            try:
                return TriageOutput.model_validate(data)
            except PydanticValidationError as exc:
                last_error = f"Schema validation failed:\n{exc}"
                if attempt + 1 < 2:
                    continue
                raise PermanentError(f"ci_triage: malformed output after retry: {exc}") from exc
        raise PermanentError("ci_triage: claude loop exited without result")

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _prior_posted(self, alert: ParsedAlert) -> AuditRow | None:
        """The prior `posted` audit row for this run/message (for the
        force-supersede header). Run-keyed first, then message-keyed."""
        if alert.run_ref is not None:
            hit = await find_posted_for_run(
                self.db, repo=alert.run_ref.repo, run_id=alert.run_ref.run_id
            )
            if hit is not None:
                return hit
        return await find_posted_for_message(
            self.db, channel_id=alert.channel_id, message_ts=alert.message_ts
        )

    async def _maybe_post_no_evidence_note(self, alert: ParsedAlert, *, reason: str) -> None:
        """Post a minimal "bot saw it, no evidence" note when configured. Best-
        effort — a note-post failure must not fail the (already-skipped) event.
        Default off under dry_run; flipped on at the P3 thread promotion."""
        if not self.config.post_no_evidence_note:
            return
        target, thread_ts = self._post_target(alert)
        run_link = (
            f"https://github.com/{alert.run_ref.repo}/actions/runs/{alert.run_ref.run_id}"
            if alert.run_ref
            else "(no run link)"
        )
        text = f"CI Triage: alert를 확인했지만 {reason} — 수동 triage가 필요합니다.\n🔗 {run_link}"
        try:
            await self.slack.post_message(
                target, text, thread_ts=thread_ts, username="CI Triage", icon_emoji=":robot_face:"
            )
        except Exception as exc:
            # best-effort note; a failure must never fail the already-skipped event.
            _log.info("ci_triage.no_evidence_note_failed", error=repr(exc))

    async def _resolve_dut_window(
        self, run_ref: RunRef, log_text: str, *, now: datetime
    ) -> LokiWindow | None:
        """Resolve the Loki host + window for a run with no host in the alert.
        Tier 2: the DUT host most-mentioned in the failed-job log. Tier 3: the
        failed job's runner_name (runner == DUT, e.g. atom-test). Window from the
        failed jobs' start/complete times (ms); None when no host is found."""
        hosts = alert_parse.extract_dut_hosts(log_text)
        host = hosts[0] if hosts else None
        try:
            jobs: list[Any] = await self.gh.failed_jobs(run_ref.repo, run_ref.run_id)
        except Exception as exc:
            _log.info("ci_triage.failed_jobs_lookup_failed", error=repr(exc))
            jobs = []
        if host is None:
            runners = [j.runner_name for j in jobs if getattr(j, "runner_name", None)]
            host = runners[0] if runners else None
        if host is None:
            return None
        starts = [_iso_to_ms(getattr(j, "started_at", None)) for j in jobs]
        ends = [_iso_to_ms(getattr(j, "completed_at", None)) for j in jobs]
        start_vals = [int(s) for s in starts if s]
        end_vals = [int(e) for e in ends if e]
        return LokiWindow(
            host=host,
            start=str(min(start_vals)) if start_vals else None,
            end=str(max(end_vals)) if end_vals else None,
        )

    async def _cross_run_evidence(self, run_ref: RunRef) -> CrossRunResult | None:
        """Compare this run against recent sibling runs of the same workflow
        (P1). Best-effort: any failure (no workflow_id, gh error) → None, which
        the caller renders as "no comparison" and never lets confidence rise on.
        Two gh calls: run_meta → list_workflow_runs."""
        try:
            meta = await self.gh.run_meta(run_ref.repo, run_ref.run_id)
            workflow_id = getattr(meta, "workflow_id", None)
            if not workflow_id:
                return None
            runs = await self.gh.list_workflow_runs(
                run_ref.repo,
                workflow_id=str(workflow_id),
                per_page=self.config.cross_run_window,
            )
        except Exception as exc:
            _log.info("ci_triage.cross_run_failed", error=repr(exc))
            return None
        result = analyze_cross_run(head_sha=getattr(meta, "head_sha", None), runs=runs)
        _log.info(
            "ci_triage.cross_run",
            verdict=result.verdict,
            others_failed=result.others_failed,
            others_total=result.others_total,
        )
        return result

    async def _recurrence_line(
        self, *, signature: str, message_ts: str, now: datetime
    ) -> str | None:
        """P2: "재발 7일 N회" when prior posted triages share the signature.
        Audit-only (no secrets); best-effort."""
        if not self.config.recurrence_enabled or not signature:
            return None
        since = (now - timedelta(days=self.config.recurrence_window_days)).isoformat()
        try:
            prior = await count_recent_by_signature(
                self.db, signature=signature, since_iso=since, exclude_message_ts=message_ts
            )
        except Exception as exc:
            _log.info("ci_triage.recurrence_failed", error=repr(exc))
            return None
        if prior < 1:
            return None
        return f"재발 {self.config.recurrence_window_days}일 {prior + 1}회"

    async def _ticket_search(self, *, dut_host: str | None, triage: TriageOutput) -> list[str]:
        """P2/P4: open Jira (SSWCI/SDOC) + Linear (DOLIN) issues matching the
        host/signature, as compact labels. Opt-in + best-effort — no client or
        any error → []."""
        if not self.config.ticket_search_enabled:
            return []
        term = dut_host or _top_token(triage.headline)
        if not term:
            return []
        labels = await self._jira_tickets(term)
        labels += await self._linear_tickets(term)
        return labels[:3]

    async def _jira_tickets(self, term: str) -> list[str]:
        if self.jira is None:
            return []
        projects = ", ".join(f'"{p}"' for p in self.config.ticket_jira_projects)
        safe = term.replace('"', " ").replace("\\", " ")
        jql = (
            f"project in ({projects}) AND statusCategory != Done"
            f' AND text ~ "{safe}" ORDER BY created DESC'
        )
        try:
            page = await self.jira.search_jql(jql=jql, fields=["summary", "status"], max_results=2)
        except Exception as exc:
            _log.info("ci_triage.jira_search_failed", error=repr(exc))
            return []
        base = str(getattr(self.jira, "base_url", "") or "").rstrip("/")
        out: list[str] = []
        for i in page.issues[:2]:
            label = f"{i.key} ({i.status_name or 'open'})"
            out.append(f"<{base}/browse/{i.key}|{label}>" if base else label)
        return out

    async def _linear_tickets(self, term: str) -> list[str]:
        if self.linear is None:
            return []
        try:
            issues = await self.linear.search_issues(term, limit=3)
        except Exception as exc:
            _log.info("ci_triage.linear_search_failed", error=repr(exc))
            return []
        out: list[str] = []
        for i in issues:
            if not i.is_open:
                continue
            label = f"{i.identifier} ({i.state_name or 'open'})"
            out.append(f"<{i.url}|{label}>" if i.url else label)
        return out[:2]

    async def _loki_evidence(self, window: LokiWindow, *, now: datetime) -> tuple[str, str | None]:
        """Fetch error-class log lines for the alert's host+window from Loki.

        Returns (text, error_label). Never raises — the LokiClient maps network
        failures to an error label, and an absent client / empty result degrades
        to an empty string (the prompt rule then yields confidence:low). LogQL
        mirrors ssw-bundle's ai_triage fetcher: kernel + rebellions-* streams,
        filtered by a device-error regex at query time. (fwlog/smclog streams are
        IP-labelled, not short-hostname, so this hostname-scoped query covers the
        kernel `[rbln-fwi]` pass-through, which is where device errors surface.)"""
        if self.loki is None:
            return ("", "loki_absent")
        start, end = _loki_bounds(window, now=now)
        logql = (
            f'{{hostname="{_logql_escape(window.host)}",logtype=~"kernel|rebellions-.*"}}'
            f' |~ "{_DEVICE_ERR_PATTERN}"'
        )
        result = await self.loki.query_range(
            stream="kernel", logql=logql, start=start, end=end, limit=2000
        )
        if result.error is not None or result.slice is None:
            return ("", f"loki:{result.error}")
        if not result.slice.lines:
            return ("", "loki_empty")
        text = redact_text("\n".join(result.slice.lines))
        return (text[:_LOKI_MAX_CHARS], None)

    def _post_target(self, alert: ParsedAlert) -> tuple[str, str | None]:
        """Return (channel_id, thread_ts). dry_run → test channel, no thread;
        thread → original channel, reply in the alert thread (P3)."""
        if (
            self.config.post_target == "thread"
            and alert.channel_id
            and not alert.channel_id.startswith("manual")
        ):
            return (alert.channel_id, alert.message_ts)
        return (self.config.dry_run_channel, None)

    async def _audit(
        self,
        event: Event,
        alert: ParsedAlert,
        *,
        status: str,
        now: datetime,
        **extra: Any,
    ) -> None:
        await insert_audit(
            self.db,
            event_id=event.id,
            channel_id=alert.channel_id,
            message_ts=alert.message_ts,
            status=status,  # type: ignore[arg-type]
            created_at=now,
            repo=alert.run_ref.repo if alert.run_ref else None,
            run_id=alert.run_ref.run_id if alert.run_ref else None,
            pr_number=alert.pr_number,
            failed_jobs=alert.failed_jobs,
            **extra,
        )
        await self.db.commit()


# ── module-level helpers ─────────────────────────────────────────────────────

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _CODE_FENCE_RE.sub("", stripped)
    return stripped.strip()


def _parse_event(event: Event) -> tuple[ParsedAlert, bool]:
    """Build a ParsedAlert + force flag from either event type."""
    payload = event.payload
    if event.type == "ci.triage.manual" or ("repo" in payload and "run_id" in payload):
        repo = str(payload.get("repo", ""))
        run_id = str(payload.get("run_id", ""))
        run_ref = RunRef(repo=repo, run_id=run_id) if repo and run_id else None
        # A manual fire may carry the source alert's thread coordinates so the
        # triage replies in that real thread under post_target="thread". Absent
        # them, fall back to synthetic ids → the post goes to dry_run_channel.
        thread_channel = str(payload.get("channel_id", ""))
        thread_ts = str(payload.get("message_ts", ""))
        return (
            ParsedAlert(
                channel_id=thread_channel or f"manual:{repo}",
                message_ts=thread_ts or run_id,
                author_id=None,
                merged_text=f"manual triage {repo} run {run_id}",
                run_ref=run_ref,
            ),
            bool(payload.get("force", False)),
        )
    # auto: re-parse the stored raw_blob deterministically.
    channel_id = str(payload.get("channel_id", ""))
    message_ts = str(payload.get("message_ts", ""))
    raw_blob = str(payload.get("raw_blob", ""))
    merged = raw_blob
    return (
        ParsedAlert(
            channel_id=channel_id,
            message_ts=message_ts,
            author_id=_opt(payload.get("author_id")),
            merged_text=merged,
            run_ref=alert_parse.extract_run_ref(merged),
            pr_number=alert_parse.extract_pr_number(merged),
            head_sha=alert_parse.extract_head_sha(merged),
            failed_jobs=alert_parse.extract_failed_jobs(merged),
            consecutive_fail_count=alert_parse.extract_consecutive_fail_count(merged),
            loki_window=alert_parse.extract_loki_window(merged),
        ),
        bool(payload.get("force", False)),
    )


def _opt(value: object) -> str | None:
    return value if isinstance(value, str) else None


_TERM_ANCHORS = ("error", "fail", "no such file", "timeout", "unable", "abort", "panic")
# Common log noise tokens that would match too many incidents — excluded so the
# distinctive tokens (golden-base, MAILBOX_4, rbln0, …) drive the wiki match.
_TERM_STOPWORDS = frozenset(
    {
        "error",
        "errors",
        "failed",
        "failure",
        "process",
        "completed",
        "exit",
        "code",
        "such",
        "file",
        "with",
        "the",
        "this",
        "that",
        "from",
        "into",
        "step",
        "premerge",
        "result",
        "directory",
        "found",
        "during",
        "after",
        "before",
        "timeout",
        "unable",
        "running",
        "stderr",
        "stdout",
        "command",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{3,}")


def _search_terms(alert: ParsedAlert, log_text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Derive wiki search terms from the failed log + failed-job names.

    `signatures` = multi-word error phrases (matched against incident
    `signature:` frontmatter, weighted highest). `phrases` = failed-job names +
    distinctive single tokens (golden-base, MAILBOX_4, rbln0). The single tokens
    are what actually substring-match incident bodies — a whole error LINE rarely
    appears verbatim in the wiki, but a distinctive token from it does (this is
    how the 2026-06-19 R1 validation matched `golden-base` → the incident)."""
    signatures: list[str] = []
    phrases: list[str] = list(alert.failed_jobs)
    for line in log_text.splitlines():
        if not any(a in line.lower() for a in _TERM_ANCHORS):
            continue
        msg = line.split("|", 1)[-1].strip()
        if len(msg.split()) >= 2:
            signatures.append(msg[:80])
        for token in _TOKEN_RE.findall(msg):
            if token.lower() not in _TERM_STOPWORDS:
                phrases.append(token)
        if len(phrases) >= 40:
            break
    return (tuple(dict.fromkeys(signatures)), tuple(dict.fromkeys(phrases)))


# Device-error regex applied at Loki query time (mirrors ssw-bundle ai_triage).
_DEVICE_ERR_PATTERN = r"(?i)(error|fail|abort|panic|timeout|unreachable|halt|0x[0-9a-f]{6})"
_LOKI_FALLBACK_HOURS = 2
_LOKI_MAX_CHARS = 8000


def _logql_escape(value: str) -> str:
    """Escape `\\` and `\"` for a LogQL string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _iso_to_ms(iso: str | None) -> str | None:
    """ISO8601 (e.g. a GitHub job `started_at`) → epoch-ms string, or None."""
    if not iso:
        return None
    try:
        return str(int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000))
    except (TypeError, ValueError):
        return None


def _ms_to_dt(ms: str | None) -> datetime | None:
    """Grafana/Loki link epoch-ms string → aware datetime, or None."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _loki_bounds(window: LokiWindow, *, now: datetime) -> tuple[datetime, datetime]:
    """Resolve [start, end) for the Loki query from the alert window, falling back
    to a fixed window ending at `now` when the alert carries no explicit range."""
    end = _ms_to_dt(window.end) or now
    start = _ms_to_dt(window.start) or (end - timedelta(hours=_LOKI_FALLBACK_HOURS))
    if start >= end:
        start = end - timedelta(hours=_LOKI_FALLBACK_HOURS)
    return (start, end)


def _render_user_message(
    alert: ParsedAlert,
    log_text: str,
    annotations: str,
    loki_text: str,
    wiki_matches: list[Any],
    cross_run: CrossRunResult | None = None,
    timeline_text: str = "",
) -> str:
    meta = [
        "## GitHub / alert metadata",
        f"- channel: {alert.channel_id}",
        f"- repo: {alert.run_ref.repo if alert.run_ref else '(none)'}",
        f"- run_id: {alert.run_ref.run_id if alert.run_ref else '(none)'}",
        f"- PR: {alert.pr_number if alert.pr_number is not None else '(none)'}",
        f"- head SHA: {alert.head_sha or '(none)'}",
        f"- failed jobs: {', '.join(alert.failed_jobs) or '(none)'}",
        f"- consecutive fails: {alert.consecutive_fail_count if alert.consecutive_fail_count is not None else '(none)'}",
    ]
    wiki_block = (
        "\n".join(
            f"- {m.path} (signature_match={m.signature_matched}): {m.snippet}" for m in wiki_matches
        )
        or "(no wiki matches)"
    )
    return (
        "\n".join(meta)
        + (
            "\n\n## GitHub check-run annotations (failure reasons; high-signal)\n" + annotations
            if annotations
            else ""
        )
        + "\n\n## Failed-job log (primary evidence; error-anchored, redacted)\n"
        + (log_text or "(no run log available)")
        + ("\n\n## Loki (device-level)\n" + loki_text if loki_text else "")
        + ("\n\n" + timeline_text if timeline_text else "")
        + ("\n\n" + cross_run.prompt_block() if cross_run is not None else "")
        + "\n\n## OnCall wiki runbook matches (supporting evidence only)\n"
        + wiki_block
        + "\n\nTriage this CI failure. Log is primary evidence; wiki is supporting."
        " Do not assert a wiki link without a matching log anchor. If evidence is"
        " insufficient, set attribution=unknown / confidence=low."
    )


def _supersede_header(prior: AuditRow) -> str:
    """Force-supersede header. The Slack adapter has no chat.update/delete, so a
    force posts a NEW message and the prior one stays (documented in RUNBOOK)."""
    when = prior.created_at.strftime("%H:%M:%S")
    return f"_Updated triage (supersedes earlier comment posted at {when} UTC)_\n\n"


# Signature derivation (P2 recurrence): mask host-/instance-specific tokens so
# the SAME kind of failure on different hosts collapses to one signature.
_SIG_HOST_RE = re.compile(r"\b(?:ssw-\S+|[a-z]+-\d+|0x[0-9a-f]+|\d+)\b", re.IGNORECASE)
_SIG_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_SIG_STOPWORDS = frozenset({"fail", "failed", "error", "the", "with", "and", "for"})


def _failure_signature(headline: str, classification: str) -> str:
    """Host-agnostic normalized failure key for recurrence matching.

    Masks hostnames / instance numbers / hex from the headline, keeps the
    distinctive lowercased tokens (order-independent), and prefixes the
    classification. Same KIND of failure on different hosts → same signature.
    Empty when no distinctive token survives (→ no recurrence claim)."""
    masked = _SIG_HOST_RE.sub(" ", headline.lower())
    tokens = sorted({t for t in _SIG_TOKEN_RE.findall(masked) if t not in _SIG_STOPWORDS})
    key = "-".join(tokens[:6])
    return f"{classification}:{key}" if key else ""


def _top_token(headline: str) -> str:
    """Longest distinctive (non-host) token of the headline — a ticket-search
    fallback term when no DUT host is known."""
    masked = _SIG_HOST_RE.sub(" ", headline)
    cands = [t for t in _SIG_TOKEN_RE.findall(masked) if t.lower() not in _SIG_STOPWORDS]
    return max(cands, key=len) if cands else ""


def _ticket_draft(t: TriageOutput, tickets: list[str], *, enabled: bool) -> str | None:
    """P4: suggest a SSWCI bug stub for a confident infra_env failure that has no
    matching open ticket. Suggest-only — the bot never files it; the operator
    one-clicks "create from message". None when not applicable."""
    if not enabled or tickets or t.attribution != "infra_env" or t.confidence == "low":
        return None
    return f'신규 SSWCI bug 제안: "{t.headline}" (infra_env/{t.owner_area})'


_CIRCLED_RE = re.compile(r"[①-⑳]")  # ① .. ⑳

# Functional (not decorative) icons — the attribution glyph and the rerun verdict
# are the two signals on-call scans first. Everything else is a text label.
_ATTR_EMOJI = {
    "infra_env": "🔧",
    "product_regression": "🐛",
    "flaky": "🎲",
    "unknown": "❓",
}
_ATTR_KO = {
    "infra_env": "인프라/환경",
    "product_regression": "제품 회귀",
    "flaky": "flaky (불안정)",
    "unknown": "분류 불가",
}
_CONF_KO = {"low": "낮음", "medium": "보통", "high": "높음"}


def _action_items(text: str) -> list[str]:
    """Split a recommended-action blob into discrete steps for a numbered list.
    The model emits circled numerals (①②③…); fall back to newline- or
    'N.'-delimited, or the whole string when no enumerator is present."""
    text = text.strip()
    if _CIRCLED_RE.search(text):
        parts = _CIRCLED_RE.split(text)
    else:
        parts = re.split(r"\n+|(?:(?<=\s)|^)\d{1,2}[.)]\s+", text)
    items: list[str] = []
    for part in parts:
        cleaned = part.strip().lstrip(".) 、。　").strip()
        if cleaned:
            items.append(cleaned)
    return items or [text]


_RERUN_KR = {
    "safe_to_rerun": "✅ rerun 가능",
    "do_not_rerun": "🛑 rerun 금지",
    "needs_investigation": "🔍 조사 필요 · rerun 보류",
    "unknown": "❔ rerun 판단 불가",
}


def _evidence_box(evidence: tuple[Any, ...], *, limit: int) -> list[str]:
    """The log-grounded evidence as a Slack code box — each entry is a `# citation`
    comment line followed by the real log quote. This is the trust anchor: on-call
    verifies the bot's classification against the actual lines, not its word."""
    if not evidence:
        return []
    body: list[str] = []
    for e in evidence[:limit]:
        citation = e.citation.strip()
        if citation:
            body.append(f"# {citation}")
        body.append(_truncate(e.quote.strip(), 200))
    return ["*근거*", "```", *body, "```"]


def _render_slack_body(
    alert: ParsedAlert,
    t: TriageOutput,
    wiki_matches: list[WikiMatch],
    *,
    cross_run: str | None = None,
    recurrence: str | None = None,
    tickets: list[str] | None = None,
    ticket_draft: str | None = None,
) -> str:
    """Decision-first mrkdwn built around the on-call reading order:
    *내 문제야?* (head: attribution + owner) → *rerun 돼?* (판단) → *왜?* (원인) →
    *진짜 뭐가 터졌나?* (근거 code box) → *전에 본 거?* (맥락) → *어디 파나?* (run).

    `likely_cause` / `log_evidence` are surfaced on EVERY call (the model produces
    them each time; hiding them threw away the analysis). Confidence only controls
    DEPTH — a low-confidence / `unknown` call adds secondary action items, the full
    summary, wiki refs, and a "사람이 검증" prompt that feeds the reaction loop.

    `cross_run` (P1), `recurrence`/`tickets`/`ticket_draft` (P2/P4) are optional
    high-signal context lines populated by later evidence stages; absent →
    omitted."""
    emoji = _ATTR_EMOJI.get(t.attribution, "•")
    attr_ko = _ATTR_KO.get(t.attribution, t.attribution)
    conf_ko = _CONF_KO.get(t.confidence, t.confidence)
    actions = _action_items(t.recommended_action)
    detailed = t.confidence == "low" or t.attribution == "unknown"

    # ① 내 문제야? — verdict meta + bold headline.
    owner = f" · 담당 {t.owner_area}" if t.owner_area not in ("Unknown", "") else ""
    lines = [f"{emoji} {attr_ko} · 신뢰도 {conf_ko}{owner}", f"*{t.headline}*"]

    # ② rerun 돼? — the decision, with the top action inline.
    rerun_kr = _RERUN_KR.get(t.rerun_advice, t.rerun_advice)
    decision = f"*판단:* {rerun_kr}"
    if actions:
        decision += f" — {_truncate(actions[0], 120)}"
    lines.append(decision)

    # ③ 왜? — cause (and known remedy when the playbook gives one). Always shown.
    lines.append(f"*원인:* {_truncate(t.likely_cause.strip(), 240)}")
    if t.known_remedy and t.known_remedy.strip():
        lines.append(f"*해법:* {_truncate(t.known_remedy.strip(), 240)}")

    # ④ 진짜 뭐가 터졌나? — real log lines in a code box (trust anchor).
    lines += _evidence_box(t.log_evidence, limit=3 if detailed else 2)

    # ⑤ 전에 본 거? — cross-run / recurrence / tickets context.
    ctx = " · ".join(p for p in (cross_run, recurrence) if p)
    if ctx:
        lines.append(ctx)
    if tickets:
        lines.append("🎫 " + " · ".join(tickets))
    elif ticket_draft:
        lines.append(ticket_draft)

    # Detail block — only when on-call must investigate by hand.
    if detailed:
        if actions[1:]:
            lines.append("*다음 확인:*")
            lines += [f"• {_truncate(step, 120)}" for step in actions[1:]]
        wiki_lines = [m.path.rsplit("/", 1)[-1] for m in wiki_matches[:2]]
        if wiki_lines:
            lines.append("참고: " + " · ".join(wiki_lines))
        lines.append(f"요약: {_truncate(t.summary, 240)}")
        if t.confidence == "low" or t.needs_human:
            lines.append("⚠️ 신뢰도 낮음 — 사람이 검증 필요 (맞으면 ✅ / 틀리면 ❌)")

    # ⑥ 어디 파나? — run link.
    lines.append(_footer_line(alert))
    return "\n".join(lines)


def _footer_line(alert: ParsedAlert) -> str:
    """One-line footer: run link (repo #PR), or a plain signature when no run."""
    if alert.run_ref is None:
        return "daeyeon-bot"
    run_link = f"https://github.com/{alert.run_ref.repo}/actions/runs/{alert.run_ref.run_id}"
    repo_short = alert.run_ref.repo.rsplit("/", 1)[-1]
    pr = f" #{alert.pr_number}" if alert.pr_number is not None else ""
    return f"🔗 <{run_link}|{repo_short}{pr}>"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


_SCHEMA_APPENDIX = """

---

You are triaging the CI failure described below. Output ONLY a JSON object that
matches this exact shape. No prose before or after, no Markdown code fence.

Required keys:
  - `attribution`     : "infra_env" | "product_regression" | "flaky" | "unknown"
  - `classification`  : "infra" | "environment" | "test_failure" | "device_failure"
                        | "build_failure" | "dependency" | "timeout" | "flaky"
                        | "permission" | "unknown"
  - `owner_area`      : "DevOps" | "SysFw" | "SysSol" | "Connectivity" | "Driver"
                        | "HW" | "Unknown"
  - `confidence`      : "low" | "medium" | "high"
  - `headline`        : str — ONE terse line (<= 80 chars), the crux: host /
                        component + the error signature (e.g.
                        "ssw-smci-16 IOMMU IOTLB_INV_TIMEOUT"). A phrase, NOT a
                        sentence. This is the Slack head line; keep it scannable.
  - `summary`         : str — one or two sentences (Korean prose OK; keep English
                        technical terms / log lines verbatim). Shown only on
                        low-confidence / unknown calls, so be concise.
  - `log_evidence`    : list[{quote, citation}] — REQUIRED when attribution is not
                        "unknown". `quote` MUST be a real line from the log above.
  - `wiki_matches`    : list[{path, why}] — incident/playbook references you used;
                        empty is fine. Only cite a wiki match if a log line
                        actually supports it.
  - `likely_cause`    : str
  - `known_remedy`    : str | null — from the recovery playbook when matched.
  - `recommended_action` : str — what on-call should do next.
  - `rerun_advice`    : "safe_to_rerun" | "do_not_rerun" | "needs_investigation"
                        | "unknown"
  - `needs_human`     : bool — true whenever you cannot confidently diagnose.

Rules: the failed-job log is PRIMARY evidence; the wiki is SUPPORTING. Never
assert a wiki link without a matching log anchor. If evidence is insufficient,
set attribution="unknown" and confidence="low".
"""


__all__ = ["MANIFEST", "CiTriageHandler", "PauseGuard"]
