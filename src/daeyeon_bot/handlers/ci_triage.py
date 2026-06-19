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
from daeyeon_bot.core.ci_triage.types import LokiWindow, ParsedAlert, RunRef, WikiMatch
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
    async def run_view_log_failed(self, repo: str, run_id: str) -> str: ...


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
    pause_guard: PauseGuard = _no_pause

    async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
        budget = float(self.config.timeout_seconds)
        try:
            return await asyncio.wait_for(self._handle_inner(event, ctx), timeout=budget)
        except TimeoutError as exc:
            raise TransientError(f"ci_triage exceeded {budget}s budget (asyncio.wait_for)") from exc

    async def _handle_inner(self, event: Event, ctx: HandlerContext) -> HandlerResult:
        await self.pause_guard()
        now = ctx.clock.now() if hasattr(ctx, "clock") else datetime.now(tz=UTC)
        alert, force = _parse_event(event)

        # Gate: no machine-readable run link AND no Loki window → nothing to triage.
        if alert.run_ref is None and alert.loki_window is None:
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

        # ── EVIDENCE: run-log path (primary) ──────────────────────────────────
        log_text = ""
        gh_error: str | None = None
        if alert.run_ref is not None:
            try:
                raw = await self.gh.run_view_log_failed(alert.run_ref.repo, alert.run_ref.run_id)
            except RunLogUnavailableError as exc:
                gh_error = f"log_unavailable:{str(exc)[:80]}"
                # If a Loki window is present, the device-level path still has
                # evidence; otherwise the run is unrecoverable → skip (not retry).
                if alert.loki_window is None:
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

        # ── EVIDENCE: device-level Loki path (best-effort) ────────────────────
        loki_text = _loki_placeholder(alert.loki_window)

        # ── OnCall wiki ───────────────────────────────────────────────────────
        wiki_error: str | None = None
        wiki_matches: list[WikiMatch] = []
        try:
            refresh: WikiRefresh = await self.oncall_wiki.ensure_fresh()
            if refresh.error:
                wiki_error = refresh.error
            if refresh.available:
                signatures, phrases = _search_terms(alert, log_text)
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
            loki_text=loki_text,
            wiki_matches=wiki_matches,
        )
        triage = enforce_confidence_floor(
            triage, has_strong_anchor=has_strong_anchor, has_wiki_match=has_wiki
        )

        # ── Render + redaction guard ──────────────────────────────────────────
        body = _render_slack_body(alert, triage, wiki_matches)
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
        loki_text: str,
        wiki_matches: list[WikiMatch],
    ) -> TriageOutput:
        """Two in-call attempts. Second parse/validate failure → DeadLetter
        (PermanentError). This is an in-call loop, NOT a dispatcher retry."""
        system_prompt = persona.body + _SCHEMA_APPENDIX
        user_message = _render_user_message(alert, log_text, loki_text, wiki_matches)
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


def _loki_placeholder(window: LokiWindow | None) -> str:
    """Device-level Loki evidence is wired in P1 only as a passthrough note; the
    full LokiClient query path is exercised via the jira_triage adapter and will
    be enabled here once a window-bearing alert is triaged. P1 manual-fire uses
    the run-log path."""
    if window is None:
        return ""
    parts = [f"host={window.host}"]
    if window.start:
        parts.append(f"from={window.start}")
    if window.end:
        parts.append(f"to={window.end}")
    return "Loki window available (" + ", ".join(parts) + ")"


def _render_user_message(
    alert: ParsedAlert,
    log_text: str,
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
        + "\n\n## Failed-job log (primary evidence; error-anchored, redacted)\n"
        + (log_text or "(no run log available)")
        + ("\n\n## Loki (device-level)\n" + loki_text if loki_text else "")
        + "\n\n## OnCall wiki runbook matches (supporting evidence only)\n"
        + wiki_block
        + "\n\nTriage this CI failure. Log is primary evidence; wiki is supporting."
        " Do not assert a wiki link without a matching log anchor. If evidence is"
        " insufficient, set attribution=unknown / confidence=low."
    )


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
