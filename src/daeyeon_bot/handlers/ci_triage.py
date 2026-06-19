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
from daeyeon_bot.handlers.ci_triage_parsing import error_anchored_windows
from daeyeon_bot.handlers.ci_triage_schemas import TriageOutput, enforce_confidence_floor
from daeyeon_bot.infra import alert_parse
from daeyeon_bot.infra.ci_triage_audit import (
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

        # Gate: no machine-readable run link AND no Loki window → nothing to triage.
        if alert.run_ref is None and alert.loki_window is None:
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
        if loki_window is not None:
            loki_text, loki_err = await self._loki_evidence(loki_window, now=now)
            if loki_err:
                _log.info("ci_triage.loki_degraded", host=loki_window.host, error=loki_err)

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
        )
        triage = enforce_confidence_floor(
            triage, has_strong_anchor=has_strong_anchor, has_wiki_match=has_wiki
        )

        # ── Render (+ force-supersede header) + redaction guard ───────────────
        body = _render_slack_body(alert, triage, wiki_matches)
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
    ) -> TriageOutput:
        """Two in-call attempts. Second parse/validate failure → DeadLetter
        (PermanentError). This is an in-call loop, NOT a dispatcher retry."""
        system_prompt = persona.body + _SCHEMA_APPENDIX
        user_message = _render_user_message(alert, log_text, annotations, loki_text, wiki_matches)
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
        text = (
            f"🤖 CI Triage: alert를 확인했지만 {reason} — 수동 triage가 필요합니다."
            f"  {run_link}\n🤖 automated first-pass (daeyeon-bot)"
        )
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
        return (
            ParsedAlert(
                channel_id=f"manual:{repo}",
                message_ts=run_id,
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


def _render_slack_body(alert: ParsedAlert, t: TriageOutput, wiki_matches: list[WikiMatch]) -> str:
    """Header-first: the actionable verdict (lines 1-3) is never truncated; only
    the body block below it is budget-trimmed."""
    run_link = (
        f"https://github.com/{alert.run_ref.repo}/actions/runs/{alert.run_ref.run_id}"
        if alert.run_ref
        else "(no run link)"
    )
    wiki_line = ", ".join(m.path for m in wiki_matches[:3]) or "none"
    header = [
        f"*{t.attribution}* · {t.owner_area} · confidence *{t.confidence}*",
        f"action: {t.recommended_action}  |  rerun: {t.rerun_advice}",
        f"{(alert.run_ref.repo if alert.run_ref else '?')}"
        f" · PR {alert.pr_number if alert.pr_number is not None else '-'}"
        f" · jobs: {', '.join(alert.failed_jobs) or '-'}  |  {run_link}",
    ]
    body = [
        "",
        f"summary: {t.summary}",
        f"classification: {t.classification}",
        f"likely cause: {t.likely_cause}",
        f"wiki match: {wiki_line}",
    ]
    if t.known_remedy:
        body.append(f"known remedy: {t.known_remedy}")
    body_block = _truncate("\n".join(body), 2400)
    footer = "\n🤖 automated first-pass (daeyeon-bot)"
    return "\n".join(header) + body_block + footer


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
  - `summary`         : str — one or two sentences (Korean prose OK; keep English
                        technical terms / log lines verbatim).
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
