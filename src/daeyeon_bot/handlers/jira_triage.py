"""Jira regression-failure triage handler (T039).

Consumes `jira.assigned` (auto, from `jira_assigned` trigger) and
`jira.triage.manual` (CLI) events. The full pipeline per
`specs/002-jira-triage-bot/data-model.md` §4:

    (a) jira.issue_get → title parse → title miss = audit
        `skipped_not_regression_failure`
    (b) load persona (failure → DeadLetter)
    (c) parent Epic fetch → branch + commit; missing →
        audit `skipped_missing_metadata`
    (d) audit history; if status='posted' + !force →
        audit `skipped_already_triaged`
    (e) ssw_bundle.ensure_checkout (UnresolvableCommitError /
        SubmoduleInitError → corresponding skip audits)
    (f) grep `<tc>.robot`; gather product_code excerpts
    (g) host_resolver.resolve(hostname)
    (h) parallel: Loki x4 + SSH fetch; per-channel fails go in audit
    (i) build Run Snapshot
    (j) call Claude with `persona_body + JSON-schema appendix`
        + snapshot user message; parse + validate; retry once
    (k) verify every `evidence.quote` appears verbatim in the snapshot
    (l) redact every prose field (symptom, layer_rationale, next_data,
        evidence.quote, suspected_duplicates.basis) — match → DeadLetter
    (m) build wiki-markup body (supersede header when force + prior)
    (n) jira.post_comment
    (o) audit row 'posted' + optional record_supersede

Per FR-031, the entire pipeline runs under
`asyncio.wait_for(timeout=config.timeout_seconds)`. First timeout is a
TransientError → Retry; second is PermanentError → DeadLetter.

Errors from the infra wrappers are passed through to the dispatcher
without further translation; the dispatcher's exception mapping turns
`AuthError`/`RateLimitError`/`TransientError`/`PermanentError` into
halt/Retry/DeadLetter.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast, runtime_checkable

import structlog
from pydantic import ValidationError as PydanticValidationError

from daeyeon_bot.app.config import JiraTriageHandlerEntry, LokiConfig
from daeyeon_bot.core.errors import (
    PermanentError,
    TransientError,
    ValidationError,
)
from daeyeon_bot.core.events import Event
from daeyeon_bot.core.jira_triage.audit import AuditRow
from daeyeon_bot.core.jira_triage.types import (
    EpicMeta,
    EvidenceItem,
    LokiSlice,
    PostedComment,
    ProductCodeFile,
    RunMeta,
    RunSnapshot,
    SshArtifact,
    SshDumpLocation,
    SuspectedDuplicate,
    TicketRef,
    TimeWindow,
    TitleParse,
    TriageDraft,
)
from daeyeon_bot.core.manifest import HandlerManifest
from daeyeon_bot.core.persona import Persona
from daeyeon_bot.core.protocols import HandlerContext
from daeyeon_bot.core.results import Ack, HandlerResult
from daeyeon_bot.handlers.jira_triage_parsing import (
    extract_error_log,
    parse_ssh_url,
    parse_timestamps,
    parse_title,
)
from daeyeon_bot.handlers.jira_triage_schemas import TriageOutput
from daeyeon_bot.infra import jira_markup
from daeyeon_bot.infra.jira_client import FieldDiscovery, IssueDetail, JiraIdentity
from daeyeon_bot.infra.jira_triage_audit import find_latest, insert_audit, record_supersede
from daeyeon_bot.infra.logging import redact_with_provenance
from daeyeon_bot.infra.loki import LokiQueryBuilder, LokiQueryResult
from daeyeon_bot.infra.persona_loader import PersonaLoader
from daeyeon_bot.infra.source_grep import extract_tokens
from daeyeon_bot.infra.ssw_bundle import SubmoduleInitError, UnresolvableCommitError

_log = structlog.get_logger(__name__)

MANIFEST = HandlerManifest(
    name="jira_triage",
    idempotent=True,
    dedup_ttl=timedelta(days=1),
    side_effect_key=None,
    concurrency=1,
    accepts=("jira.assigned", "jira.triage.manual"),
)

# Schema appendix Claude sees after the persona body. Mirrors
# `contracts/claude-triage-output.md` §2.
_SCHEMA_APPENDIX = """

---

You are triaging the Jira ticket below. Output ONLY a JSON object that
matches this exact shape. No prose before or after, no Markdown code
fence — just the JSON object on stdout.

Required keys:
  - `symptom`         : str — one-sentence description of what failed
                        (Korean prose OK; preserve English technical
                        terms verbatim — e.g. "rblnWaitJob TIMEDOUT").
  - `evidence`        : list[{source, quote, citation}] — see below.
  - `domain`          : Driver | SysFw | CpFw | SysSol | DevOps |
                        Connectivity | unknown.
  - `layer_rationale` : str — one-sentence justification for the chosen
                        domain, citing the strongest evidence lines.
  - `next_data`       : list[str] — concrete next-step suggestions, each
                        a short imperative (commands, files to collect,
                        hosts to re-run on). Max 10 items.
  - `severity`        : sev1 | sev2 | sev3 | unknown.
  - `needs_human`     : bool — set true whenever you cannot confidently
                        diagnose. The operator reviews these.

Optional keys:
  - `suspected_duplicates` : list[{key, basis}] — max 5. Best-effort,
                             NOT verified by the bot.

Field constraints:
  - `evidence` MUST be non-empty whenever `domain != "unknown"`. If you
    cannot find evidence, set `domain="unknown"` + `needs_human=true`.
  - `evidence[*].source` ENUM: `loki.fwlog` | `loki.smclog` |
    `loki.kernel` | `loki.syslog` | `ssh.output_xml` | `ssh.dmesg` |
    `ssh.console` | `test_code` | `product_code` | `ticket.error_log`.
  - `evidence[*].quote` MUST appear verbatim in the corresponding
    Run Snapshot section. The bot rejects fabricated quotes.
  - `evidence[*].citation` formats:
    - Loki streams: ISO8601 timestamp UTC.
    - SSH artifacts: `ssh.<filename>:<line>`.
    - Source files: `<repo-relative path>:<line>`.
    - Ticket error log: `ticket.error_log:<line>`.
"""


# Async pause guard — same shape as pr_review's. Raises QuotaError when paused.
PauseGuard = Callable[[], Awaitable[None]]


async def _no_pause() -> None:
    return None


@runtime_checkable
class _JiraClient(Protocol):
    async def issue_get(self, key: str, *, expand: list[str] | None = ...) -> IssueDetail: ...
    async def post_comment(self, key: str, *, body_wiki: str) -> PostedComment: ...


@runtime_checkable
class _LokiClient(Protocol):
    async def query_range(
        self,
        *,
        stream: Any,
        logql: str,
        start: datetime,
        end: datetime,
        limit: int = ...,
    ) -> LokiQueryResult: ...


@runtime_checkable
class _SshClient(Protocol):
    async def fetch_directory(
        self,
        *,
        host: str,
        remote_path: str,
        globs: list[str],
    ) -> Any: ...


@runtime_checkable
class _SswBundleClient(Protocol):
    async def ensure_clone(self) -> None: ...
    async def ensure_checkout(self, *, branch: str, commit_sha: str) -> None: ...
    def read_file(self, relative_path: str) -> str | None: ...
    def grep_test_case(self, *, tc_name: str) -> Any: ...
    async def grep_source_tokens(self, *, tokens: list[str]) -> Any: ...


@runtime_checkable
class _HostResolver(Protocol):
    def resolve(self, name: str) -> str | None: ...


_FALLBACK_WINDOW = timedelta(minutes=30)


@dataclass(slots=True)
class JiraTriageHandler:
    """Consumes `jira.assigned` and `jira.triage.manual` events."""

    manifest: HandlerManifest
    jira: _JiraClient
    loki: _LokiClient
    ssh: _SshClient
    ssw_bundle: _SswBundleClient
    host_resolver_factory: Callable[[], _HostResolver]
    persona_loader: PersonaLoader
    config: JiraTriageHandlerEntry
    loki_config: LokiConfig
    db: Any  # aiosqlite.Connection
    jira_identity: JiraIdentity
    field_discovery: FieldDiscovery
    pause_guard: PauseGuard = _no_pause

    async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
        budget = float(self.config.timeout_seconds)
        try:
            return await asyncio.wait_for(self._handle_inner(event, ctx), timeout=budget)
        except TimeoutError as exc:
            # First timeout → Retry. The dispatcher's retry counter does the
            # 2-strikes ladder: an attempt that times out again on retry
            # promotes to PermanentError → DeadLetter by the dispatcher's
            # mapping. Surface as TransientError here.
            raise TransientError(
                f"jira_triage exceeded {budget}s budget (asyncio.wait_for)"
            ) from exc

    # ── Inner pipeline ─────────────────────────────────────────────────────

    async def _handle_inner(  # noqa: PLR0911, PLR0915 — multi-stage pipeline; each branch documented in §4
        self, event: Event, ctx: HandlerContext
    ) -> HandlerResult:
        await self.pause_guard()
        payload = _parse_payload(event)
        now = ctx.clock.now() if hasattr(ctx, "clock") else datetime.now(tz=UTC)

        # Allowed-projects gate (defense-in-depth — JQL is canonical).
        if not _project_allowed(payload.issue_key, self.config.allowed_projects):
            await self._audit(
                event_id=event.id,
                issue_key=payload.issue_key,
                comment_seq=payload.comment_seq,
                status="skipped_not_regression_failure",
                error=(f"project not in allowed_projects={self.config.allowed_projects!r}"),
                created_at=now,
            )
            _log.info(
                "jira_triage.skipped_disallowed_project",
                issue_key=payload.issue_key,
                allowed_projects=self.config.allowed_projects,
            )
            return Ack()

        # (a) Fetch ticket; parse title.
        issue = await self.jira.issue_get(payload.issue_key, expand=["names", "renderedFields"])
        title = parse_title(issue.summary)
        if title is None:
            await self._audit(
                event_id=event.id,
                issue_key=payload.issue_key,
                comment_seq=payload.comment_seq,
                status="skipped_not_regression_failure",
                error=f"title regex miss: {issue.summary!r}",
                created_at=now,
            )
            _log.info(
                "jira_triage.skipped_not_regression_failure",
                issue_key=payload.issue_key,
                title=issue.summary,
            )
            return Ack()

        # (b) Load persona.
        try:
            persona = self.persona_loader.load(
                self.config.persona_skill or "",
                min_chars=self.config.min_persona_chars,
            )
        except ValidationError as exc:
            await self._audit(
                event_id=event.id,
                issue_key=payload.issue_key,
                tc_name=title.tc_name,
                hostname=title.hostname,
                comment_seq=payload.comment_seq,
                status="failed",
                persona_skill=self.config.persona_skill,
                error=str(exc),
                created_at=now,
            )
            # DeadLetter via raising — dispatcher's mapping handles it.
            raise

        # (c) Parent Epic → branch + commit.
        epic, missing = await self._resolve_epic(issue)
        if epic is None:
            await self._audit(
                event_id=event.id,
                issue_key=payload.issue_key,
                parent_epic_key=issue.parent_key,
                tc_name=title.tc_name,
                hostname=title.hostname,
                comment_seq=payload.comment_seq,
                status="skipped_missing_metadata",
                missing_fields=missing,
                persona_skill=self.config.persona_skill,
                persona_mtime_ns=persona.mtime_ns,
                created_at=now,
            )
            _log.info(
                "jira_triage.skipped_missing_metadata",
                issue_key=payload.issue_key,
                missing=missing,
            )
            return Ack()

        # (d) audit history — already triaged?
        prior = await find_latest(self.db, payload.issue_key)
        if prior is not None and prior.status == "posted" and not payload.force:
            await self._audit(
                event_id=event.id,
                issue_key=payload.issue_key,
                parent_epic_key=epic.epic_key,
                tc_name=title.tc_name,
                hostname=title.hostname,
                branch=epic.branch,
                head_sha=epic.commit,
                comment_seq=payload.comment_seq,
                status="skipped_already_triaged",
                error=f"prior_comment_id={prior.comment_id}",
                persona_skill=self.config.persona_skill,
                persona_mtime_ns=persona.mtime_ns,
                created_at=now,
            )
            _log.info(
                "jira_triage.skipped_already_triaged",
                issue_key=payload.issue_key,
                prior_comment_id=prior.comment_id,
            )
            return Ack()

        # (e) ssw-bundle checkout.
        try:
            await self.ssw_bundle.ensure_checkout(branch=epic.branch, commit_sha=epic.commit)
        except UnresolvableCommitError as exc:
            await self._audit_skip(
                event=event,
                payload=payload,
                title=title,
                epic=epic,
                persona=persona,
                now=now,
                status="skipped_unresolvable_commit",
                error=str(exc),
            )
            return Ack()
        except SubmoduleInitError as exc:
            await self._audit_skip(
                event=event,
                payload=payload,
                title=title,
                epic=epic,
                persona=persona,
                now=now,
                status="skipped_submodule_failure",
                missing_fields=exc.failed_paths or (),
                error=str(exc),
            )
            return Ack()

        # (f) Locate test code.
        tc_path = self.ssw_bundle.grep_test_case(tc_name=title.tc_name)
        test_code: str | None = None
        if tc_path is not None:
            test_code = self.ssw_bundle.read_file(str(tc_path))

        # (g) DNS resolve.
        host_resolver = self.host_resolver_factory()
        host_ip = host_resolver.resolve(title.hostname)

        # SSH dump location parse + (h) parallel data collection.
        ssh_loc = parse_ssh_url(issue.description_text)
        error_log_excerpt = extract_error_log(issue.description_text)
        window = self._make_window(issue, payload)
        loki_slices, loki_error = await self._collect_loki(
            host_ip=host_ip, host_name=title.hostname, tc=title.tc_name, window=window
        )
        ssh_artifacts, ssh_error = await self._collect_ssh(ssh_loc=ssh_loc)

        # (i) Build snapshot.
        meta = RunMeta(
            ticket=TicketRef(
                project=payload.issue_key.split("-", 1)[0],
                issue_key=payload.issue_key,
                created_iso=str(issue.raw_fields.get("created", "")),
            ),
            title=title,
            epic=epic,
            window=window,
            ssh=ssh_loc,
            host_ip=host_ip,
        )
        # Source-code grep: pull distinctive identifiers from the error log
        # + Loki lines and grep the checked-out `products/` tree for them.
        # Gives the persona real implementation context (not just test_code).
        product_code = await self._collect_product_code(
            error_log=error_log_excerpt, loki_slices=loki_slices
        )

        snapshot = RunSnapshot(
            meta=meta,
            error_log_excerpt=error_log_excerpt,
            test_code=test_code,
            product_code=product_code,
            loki_slices=loki_slices,
            ssh_artifacts=ssh_artifacts,
            loki_error=loki_error,
            ssh_error=ssh_error,
        )

        # (j) Call Claude.
        await self.pause_guard()
        triage = await self._call_claude_with_retry(ctx=ctx, persona=persona, snapshot=snapshot)

        # (k) verify quotes; (l) redact; (m) build comment; (n) post.
        _verify_evidence_quotes(triage, snapshot)
        _enforce_redaction(triage)
        attachments = jira_markup.build_log_attachments(snapshot, triage)
        _enforce_attachment_redaction(attachments)
        body_wiki = _build_body(
            triage=triage,
            attachments=attachments,
            force_supersede=payload.force and prior is not None and prior.status == "posted",
            prior=prior,
        )
        posted = await self.jira.post_comment(payload.issue_key, body_wiki=body_wiki)

        # (o) Audit.
        return await self._audit_post(
            event=event,
            payload=payload,
            title=title,
            epic=epic,
            window=window,
            ssh_loc=ssh_loc,
            triage=triage,
            posted=posted,
            persona=persona,
            now=now,
            prior=prior,
            loki_error=loki_error,
            ssh_error=ssh_error,
        )

    # ── Stage helpers ──────────────────────────────────────────────────────

    async def _resolve_epic(self, issue: IssueDetail) -> tuple[EpicMeta | None, tuple[str, ...]]:
        """Resolve branch + commit from the parent Epic.

        Source order (first hit wins per field):
          1. Epic's custom field (`branch_field_id`/`commit_field_id`) — when
             the Jira tenant has those fields configured. Discovered at boot
             via `getJiraIssueTypeMetaWithFields`.
          2. Epic's description wiki markup (`*Branch*: ...` / `*Commit*: ...`).
             This is the ssw-bundle convention — `inv/test_report/jira_bug.py`
             writes branch/commit into the Bug description, and the Epic
             aggregates the run's branch/commit at the top of its description.

        Returns `(epic, ())` on success or `(None, missing)` with the list of
        unresolvable fields. The handler treats either field absent as
        `skipped_missing_metadata`.
        """
        epic_key = issue.parent_key
        if epic_key is None:
            return (None, ("parent_epic",))
        epic_issue = await self.jira.issue_get(epic_key, expand=["names"])

        # 1) custom field path
        branch_id = self.config.branch_field_id or self.field_discovery.branch_field_id
        commit_id = self.config.commit_field_id or self.field_discovery.commit_field_id
        branch_val = _str_or_none(epic_issue.raw_fields.get(branch_id)) if branch_id else None
        commit_val = _str_or_none(epic_issue.raw_fields.get(commit_id)) if commit_id else None

        # 2) description wiki-markup fallback (ssw-bundle convention)
        if branch_val is None or commit_val is None:
            parsed = _parse_epic_description(epic_issue.description_text)
            if branch_val is None:
                branch_val = parsed.get("branch")
            if commit_val is None:
                commit_val = parsed.get("commit")

        missing: list[str] = []
        if not branch_val:
            missing.append("branch")
        if not commit_val:
            missing.append("commit")
        if missing:
            return (None, tuple(missing))
        # 40-hex validation defers to ssw_bundle.ensure_checkout.
        return (
            EpicMeta(epic_key=epic_key, branch=str(branch_val), commit=str(commit_val)),
            (),
        )

    def _make_window(self, issue: IssueDetail, payload: _Payload) -> TimeWindow:
        ts = parse_timestamps(issue.description_text)
        if ts is not None:
            start, end = ts
            return TimeWindow(start_ts=_as_utc(start), end_ts=_as_utc(end), fallback=False)
        # Fallback: created_at ± 30 min.
        created_raw = str(issue.raw_fields.get("created", ""))
        if created_raw:
            try:
                created = _parse_jira_datetime(created_raw)
            except ValueError:
                created = datetime.now(tz=UTC)
        else:
            created = datetime.now(tz=UTC)
        return TimeWindow(
            start_ts=created - _FALLBACK_WINDOW,
            end_ts=created + _FALLBACK_WINDOW,
            fallback=True,
        )

    async def _collect_loki(
        self,
        *,
        host_ip: str | None,
        host_name: str,
        tc: str,
        window: TimeWindow,
    ) -> tuple[tuple[LokiSlice, ...], str | None]:
        """Issue up to 4 Loki queries in parallel. Returns slices + error label."""
        # fwlog/smclog require IP; kernel/syslog use name.
        coros: list[Any] = []
        labels: list[str] = []
        if host_ip:
            coros.append(
                self.loki.query_range(
                    stream="fwlog",
                    logql=LokiQueryBuilder.fwlog_for(host_ip=host_ip, tc_name=tc),
                    start=window.start_ts,
                    end=window.end_ts,
                )
            )
            labels.append("fwlog")
            coros.append(
                self.loki.query_range(
                    stream="smclog",
                    logql=LokiQueryBuilder.smclog_for(host_ip=host_ip, tc_name=tc),
                    start=window.start_ts,
                    end=window.end_ts,
                )
            )
            labels.append("smclog")
        coros.append(
            self.loki.query_range(
                stream="kernel",
                logql=LokiQueryBuilder.kernel_for(
                    host_name=host_name,
                    template=self.loki_config.kernel_query_template,
                ),
                start=window.start_ts,
                end=window.end_ts,
            )
        )
        labels.append("kernel")
        coros.append(
            self.loki.query_range(
                stream="syslog",
                logql=LokiQueryBuilder.syslog_for(
                    host_name=host_name,
                    template=self.loki_config.syslog_query_template,
                ),
                start=window.start_ts,
                end=window.end_ts,
            )
        )
        labels.append("syslog")

        results = cast(
            "list[LokiQueryResult]",
            await asyncio.gather(*coros, return_exceptions=False),
        )
        slices: list[LokiSlice] = []
        errors: list[str] = []
        for label, r in zip(labels, results, strict=True):
            if r.slice is not None:
                slices.append(r.slice)
            if r.error is not None:
                errors.append(f"{label}:{r.error}")
        if host_ip is None:
            errors.append("fwlog,smclog:dns_failed")
        return (tuple(slices), "; ".join(errors) if errors else None)

    async def _collect_ssh(
        self,
        *,
        ssh_loc: SshDumpLocation | None,
    ) -> tuple[tuple[SshArtifact, ...], str | None]:
        if ssh_loc is None:
            return ((), "no_url_in_body")
        result = await self.ssh.fetch_directory(
            host=ssh_loc.host,
            remote_path=ssh_loc.remote_path,
            globs=list(self.config.ssh_fetch_globs),
        )
        if result.error is not None:
            return ((), result.error)
        artifacts = tuple(
            SshArtifact(filename=a.filename, size_bytes=a.size_bytes, contents=a.contents)
            for a in result.artifacts
        )
        return (artifacts, None)

    async def _collect_product_code(
        self,
        *,
        error_log: str,
        loki_slices: tuple[LokiSlice, ...],
    ) -> tuple[ProductCodeFile, ...]:
        """Evidence-driven source grep into the checked-out ssw-bundle/products/ tree.

        Failures are logged but never raise — product_code is best-effort
        context; a grep timeout or missing tree shouldn't block triage.
        """
        texts: list[str] = [error_log]
        for slc in loki_slices:
            texts.extend(slc.lines)
        tokens = extract_tokens(texts)
        if not tokens:
            return ()
        try:
            return await self.ssw_bundle.grep_source_tokens(tokens=tokens)
        except Exception as exc:
            _log.info(
                "jira_triage.product_code_grep_failed",
                error=repr(exc),
                token_count=len(tokens),
            )
            return ()

    async def _call_claude_with_retry(
        self,
        *,
        ctx: HandlerContext,
        persona: Persona,
        snapshot: RunSnapshot,
    ) -> TriageDraft:
        """Two-attempt Claude call. Second parse/validate failure → DeadLetter."""
        system_prompt = persona.body + _SCHEMA_APPENDIX
        user_message = _render_user_message(snapshot)
        last_error: str | None = None
        for attempt in range(2):
            session = ctx.claude_session_factory()
            prompt = user_message
            if last_error is not None:
                prompt = (
                    f"{user_message}\n\n---\nYour previous response failed validation:"
                    f"\n{last_error}\nFix and return ONLY a valid JSON object."
                )
            # ClaudeSession is an async context manager — RealClaudeSession
            # spawns the SDK subprocess on __aenter__ and reaps it on __aexit__.
            async with session as s:  # type: ignore[attr-defined]
                text_obj: object = await s.query(  # type: ignore[attr-defined]
                    prompt, system=system_prompt
                )
            text: str = text_obj if isinstance(text_obj, str) else str(text_obj)  # type: ignore[arg-type]
            try:
                data = json.loads(_strip_code_fence(text))
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = f"JSON parse error: {exc}"
                if attempt + 1 < 2:
                    continue
                raise PermanentError(
                    f"claude returned malformed triage JSON after retry: {exc}"
                ) from exc
            try:
                parsed = TriageOutput.model_validate(data)
            except PydanticValidationError as exc:
                last_error = f"Schema validation failed:\n{exc}"
                if attempt + 1 < 2:
                    continue
                raise PermanentError(
                    f"claude returned malformed triage after retry: {exc}"
                ) from exc
            return _draft_from_output(parsed)
        # Unreachable — loop always returns or raises.
        raise PermanentError("claude triage loop exited without result")

    # ── Audit helpers ──────────────────────────────────────────────────────

    async def _audit(self, **kwargs: Any) -> int:
        rid = await insert_audit(self.db, **kwargs)
        await self.db.commit()
        return rid

    async def _audit_skip(
        self,
        *,
        event: Event,
        payload: _Payload,
        title: TitleParse,
        epic: EpicMeta,
        persona: Persona,
        now: datetime,
        status: Literal[
            "skipped_unresolvable_commit",
            "skipped_submodule_failure",
        ],
        missing_fields: tuple[str, ...] = (),
        error: str = "",
    ) -> None:
        await self._audit(
            event_id=event.id,
            issue_key=payload.issue_key,
            parent_epic_key=epic.epic_key,
            tc_name=title.tc_name,
            hostname=title.hostname,
            branch=epic.branch,
            head_sha=epic.commit,
            comment_seq=payload.comment_seq,
            status=status,
            missing_fields=missing_fields,
            persona_skill=self.config.persona_skill,
            persona_mtime_ns=persona.mtime_ns,
            error=error,
            created_at=now,
        )
        _log.info(
            f"jira_triage.{status}",
            issue_key=payload.issue_key,
            error=error,
        )

    async def _audit_post(
        self,
        *,
        event: Event,
        payload: _Payload,
        title: TitleParse,
        epic: EpicMeta,
        window: TimeWindow,
        ssh_loc: SshDumpLocation | None,
        triage: TriageDraft,
        posted: PostedComment,
        persona: Persona,
        now: datetime,
        prior: AuditRow | None,
        loki_error: str | None,
        ssh_error: str | None,
    ) -> HandlerResult:
        new_audit_id = await self._audit(
            event_id=event.id,
            issue_key=payload.issue_key,
            parent_epic_key=epic.epic_key,
            tc_name=title.tc_name,
            hostname=title.hostname,
            branch=epic.branch,
            head_sha=epic.commit,
            run_id=ssh_loc.run_id if ssh_loc else None,
            start_ts=window.start_ts,
            end_ts=window.end_ts,
            time_window_fallback=window.fallback,
            comment_seq=payload.comment_seq,
            status="posted",
            domain=triage.domain,
            severity=triage.severity,
            comment_id=posted.comment_id,
            posted_at=posted.posted_at,
            summary_chars=len(triage.symptom) + len(triage.layer_rationale),
            evidence_count=len(triage.evidence),
            loki_error=loki_error,
            ssh_error=ssh_error,
            persona_skill=self.config.persona_skill,
            persona_mtime_ns=persona.mtime_ns,
            created_at=now,
        )
        # Force-supersede: append prior comment_id to the prior row's history
        # AND mark our new row's prior link via superseded_comment_ids.
        if payload.force and prior is not None and prior.comment_id is not None:
            await record_supersede(
                self.db,
                new_audit_id,
                new_comment_id=posted.comment_id,
                new_posted_at=posted.posted_at,
            )
            # Also extend the prior audit row's history.
            await record_supersede(
                self.db,
                prior.id,
                new_comment_id=posted.comment_id,
                new_posted_at=posted.posted_at,
            )
            await self.db.commit()
        _log.info(
            "jira_triage.posted",
            issue_key=payload.issue_key,
            comment_id=posted.comment_id,
            domain=triage.domain,
            severity=triage.severity,
            evidence_count=len(triage.evidence),
        )
        return Ack()


# ── Free helpers ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Payload:
    issue_key: str
    force: bool
    comment_seq: str
    assignment_gen: int
    assignee_path: str


def _parse_payload(event: Event) -> _Payload:
    """Extract handler-relevant fields from the event payload (typed view)."""
    p = dict(event.payload)
    issue_key = str(p.get("issue_key", ""))
    if not issue_key:
        raise PermanentError(f"event {event.id}: missing issue_key in payload")
    if event.type == "jira.triage.manual":
        force = bool(p.get("force", False))
        comment_seq = str(
            p.get("comment_seq")
            or ("1" if not force else f"manual_{int(datetime.now(tz=UTC).timestamp())}")
        )
        return _Payload(
            issue_key=issue_key,
            force=force,
            comment_seq=comment_seq,
            assignment_gen=0,
            assignee_path="manual",
        )
    # jira.assigned
    assignment_gen = int(p.get("assignment_gen", 1))
    return _Payload(
        issue_key=issue_key,
        force=False,
        comment_seq=str(assignment_gen),
        assignment_gen=assignment_gen,
        assignee_path=str(p.get("assignee_path", "user")),
    )


# Wiki-markup `*Branch*: <value>` / `*Commit*: <value>` (ssw-bundle's
# jira_bug.py:177 convention used for Suite-Setup-Skip bug bodies).
_BRANCH_WIKI_RE = re.compile(r"\*Branch\*:\s*(?P<v>\S+)", re.IGNORECASE)
_COMMIT_WIKI_RE = re.compile(r"\*Commit\*:\s*(?P<v>[0-9a-fA-F]{7,40})", re.IGNORECASE)

# SSWCI Epic descriptions store the Commit / Branch as one table row.
# Production reads ADF (Atlassian Document Format) and `_adf_to_text`
# flattens it to per-cell newlines + `<href>` markers for link nodes:
#
#   Commit (Branch)
#   2486620<https://github.com/.../commit/2486620> (dev)
#
# The MCP-rendered markdown form (used during dev) is also accepted:
#
#   | **Commit (Branch)** | [2486620](https://.../commit/2486620) (dev) |
#
# Strategy: anchor on the "Commit (Branch)" label, consume non-hex up to
# the SHA, then non-paren content up to the trailing `(branch)` group.
# The branch capture is restricted to letter-prefixed `[\w./-]*` so it
# can't accidentally swallow a URL host.
_COMMIT_BRANCH_TABLE_RE = re.compile(
    r"Commit\s*\(Branch\)"  # label cell
    r"[^a-fA-F0-9]*?"  # whitespace / newlines / cell delimiters
    r"(?P<sha>[0-9a-fA-F]{7,40})\b"  # SHA
    r"[^()]*?"  # consume `<href>`, spaces, etc.
    r"\((?P<branch>[a-zA-Z][\w./-]*)\)",  # trailing (branch)
    re.DOTALL,
)


def _str_or_none(value: object) -> str | None:
    """Coerce raw Jira field value to non-empty str, or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_epic_description(description: str) -> dict[str, str]:
    """Extract `branch` / `commit` from an Epic description.

    Two formats supported:
      (A) ssw-bundle Suite-Setup-Skip body — wiki markup:
            *Branch*: release/v3.2
            *Commit*: 140112e9...
      (B) ssw-bundle test-run Epic body — Jira markdown table:
            | **Commit (Branch)** | [2486620](url) (dev) |

    Both branch and commit are captured if present. `commit` accepts 7-40
    hex (short SHA tolerated by `git checkout`; ssw_bundle validates).
    """
    out: dict[str, str] = {}
    if not description:
        return out
    # Format A
    bm = _BRANCH_WIKI_RE.search(description)
    if bm:
        out["branch"] = bm.group("v").strip()
    cm = _COMMIT_WIKI_RE.search(description)
    if cm:
        out["commit"] = cm.group("v").strip()
    # Format B — fills any gaps left by format A.
    tm = _COMMIT_BRANCH_TABLE_RE.search(description)
    if tm:
        out.setdefault("commit", tm.group("sha").strip())
        out.setdefault("branch", tm.group("branch").strip())
    return out


def _project_allowed(issue_key: str, allowed_projects: list[str]) -> bool:
    if not allowed_projects:
        return False
    project = issue_key.split("-", 1)[0]
    return project in allowed_projects


def _as_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC (assume naive == UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _parse_jira_datetime(raw: str) -> datetime:
    """Jira returns `2026-05-13T07:15:02.123+0000`. fromisoformat needs `+00:00`."""
    fixed = raw
    if len(fixed) >= 5 and (fixed[-5] in "+-") and fixed[-3] != ":":
        fixed = fixed[:-2] + ":" + fixed[-2:]
    return datetime.fromisoformat(fixed)


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Strip ```json ... ``` fence if Claude wrapped output despite instructions."""
    stripped = text.strip()
    m = _CODE_FENCE_RE.match(stripped)
    if m is None:
        return stripped
    return m.group(1).strip()


def _draft_from_output(parsed: TriageOutput) -> TriageDraft:
    """Convert validated Pydantic output → core dataclass."""
    return TriageDraft(
        symptom=parsed.symptom,
        evidence=tuple(
            EvidenceItem(source=e.source, quote=e.quote, citation=e.citation)
            for e in parsed.evidence
        ),
        domain=parsed.domain,
        layer_rationale=parsed.layer_rationale,
        next_data=tuple(parsed.next_data),
        severity=parsed.severity,
        suspected_duplicates=tuple(
            SuspectedDuplicate(key=d.key, basis=d.basis) for d in parsed.suspected_duplicates
        ),
        needs_human=parsed.needs_human,
    )


def _verify_evidence_quotes(triage: TriageDraft, snapshot: RunSnapshot) -> None:
    """Reject if any evidence.quote isn't a verbatim substring of its source."""
    haystack_by_source = _build_haystack(snapshot)
    for item in triage.evidence:
        haystack = haystack_by_source.get(item.source, "")
        if item.quote not in haystack:
            raise PermanentError(
                f"jira_triage: fabricated evidence quote for source={item.source!r}"
                f" — quote not found in Run Snapshot section"
            )


def _build_haystack(snapshot: RunSnapshot) -> dict[str, str]:
    """Map evidence.source → text in which `quote` must appear."""
    haystack: dict[str, str] = {}
    for slc in snapshot.loki_slices:
        haystack[f"loki.{slc.stream}"] = "\n".join(slc.lines)
    for art in snapshot.ssh_artifacts:
        # `ssh.output_xml` / `ssh.dmesg` / `ssh.console` are the canonical labels.
        label_key = art.filename.split(".")[0]
        label = (
            f"ssh.{label_key}" if label_key == "output_xml" else f"ssh.{art.filename.split('.')[0]}"
        )
        # Match the contract: filenames `output.xml` → source `ssh.output_xml`;
        # `dmesg.log` → `ssh.dmesg`; `console.log` → `ssh.console`.
        if art.filename == "output.xml":
            label = "ssh.output_xml"
        elif art.filename == "dmesg.log":
            label = "ssh.dmesg"
        elif art.filename == "console.log":
            label = "ssh.console"
        else:
            label = f"ssh.{art.filename}"
        # Aggregate across artifacts that share a label (defensive — shouldn't happen).
        prior_haystack = haystack.get(label, "")
        haystack[label] = prior_haystack + (art.contents or "") + "\n"
    if snapshot.test_code is not None:
        haystack["test_code"] = snapshot.test_code
    if snapshot.product_code:
        haystack["product_code"] = "\n".join(p.excerpt for p in snapshot.product_code)
    if snapshot.error_log_excerpt:
        haystack["ticket.error_log"] = snapshot.error_log_excerpt
    return haystack


def _enforce_redaction(triage: TriageDraft) -> None:
    """Strict redaction: any match in any prose field or quote → DeadLetter."""
    for label, text in _iter_prose_fields(triage):
        _, spans = redact_with_provenance(text)
        if spans:
            raise PermanentError(
                f"jira_triage: redaction would alter posted content in {label}"
                f" ({len(spans)} match(es))"
            )


def _iter_prose_fields(triage: TriageDraft) -> Iterator[tuple[str, str]]:
    """Yield every (label, text) the handler will post — everything must survive redaction."""
    yield ("symptom", triage.symptom)
    yield ("layer_rationale", triage.layer_rationale)
    for i, item in enumerate(triage.next_data):
        yield (f"next_data[{i}]", item)
    for i, item in enumerate(triage.evidence):
        yield (f"evidence[{i}].quote", item.quote)
    for i, dup in enumerate(triage.suspected_duplicates):
        yield (f"suspected_duplicates[{i}].basis", dup.basis)


def _build_body(
    *,
    triage: TriageDraft,
    attachments: jira_markup.LogAttachments,
    force_supersede: bool,
    prior: AuditRow | None,
) -> str:
    """Render the wiki-markup body. Supersede header prepended when applicable."""
    supersede_header: str | None = None
    if force_supersede and prior is not None and prior.posted_at is not None:
        ts = prior.posted_at.astimezone(UTC).strftime("%H:%M:%S UTC")
        supersede_header = jira_markup.supersede_header_text(ts)
    return jira_markup.build_comment(
        triage, attachments=attachments, supersede_header=supersede_header
    )


def _enforce_attachment_redaction(attachments: jira_markup.LogAttachments) -> None:
    """Named-secret guard for log excerpt blocks (FR-015 §3).

    Mirrors the PR-review two-tier policy: a Slack / AWS / JWT / Anthropic /
    GitHub-PAT match in a raw log line is a real secret leaking through our
    fetch path; refuse to post (DLQ). Entropy-only matches (long hex strings,
    UUIDs, hashes) are common in dmesg / FW logs and would generate too many
    false positives, so we leave them alone.
    """
    for label, block in attachments.expand_blocks.items():
        _, spans = redact_with_provenance(block)
        named = [(s, e, r) for s, e, r in spans if r != "entropy"]
        if named:
            reasons = sorted({r for _, _, r in named})
            raise PermanentError(
                f"jira_triage: redaction would alter posted log excerpt ({label});"
                f" reasons={reasons}"
            )


_RENDER_LINE_CAP = 200  # max lines per stream to include in user message
_RENDER_BYTE_CAP = 64_000  # overall cap


def _render_user_message(snapshot: RunSnapshot) -> str:  # noqa: PLR0915 — render is line-oriented
    """Render the Run Snapshot as a plain-text block for Claude's user message.

    Capped at ~64 KB; over-budget streams are noted as `[truncated]`.
    """
    parts: list[str] = []
    meta = snapshot.meta

    parts.append("=== Ticket ===")
    parts.append(f"Key: {meta.ticket.issue_key}")
    parts.append(f"Title: regression-test . {meta.title.hostname} . {meta.title.tc_name}")

    parts.append("")
    parts.append("=== Run meta ===")
    parts.append(f"Hostname: {meta.title.hostname}  (IP: {meta.host_ip or 'dns_failed'})")
    if meta.ssh:
        parts.append(f"Run ID: {meta.ssh.run_id}")
    parts.append(
        f"Start: {meta.window.start_ts.isoformat()}    End: {meta.window.end_ts.isoformat()}"
        f"   (fallback: {meta.window.fallback})"
    )
    parts.append(f"Branch: {meta.epic.branch}    Commit: {meta.epic.commit}")
    parts.append(f"Epic: {meta.epic.epic_key}")

    parts.append("")
    parts.append("=== Error log (from ticket body) ===")
    parts.append(snapshot.error_log_excerpt or "(empty)")

    parts.append("")
    parts.append("=== Test code ===")
    parts.append(snapshot.test_code or "(not located in suites tree)")

    parts.append("")
    parts.append("=== Product code excerpts ===")
    if snapshot.product_code:
        for pc in snapshot.product_code:
            parts.append(f"[{pc.file_path}]")
            parts.append(pc.excerpt)
    else:
        parts.append("(none — v1 does not auto-select product code)")

    parts.append("")
    parts.append("=== Loki streams ===")
    if not snapshot.loki_slices:
        parts.append("(empty)")
    for slc in snapshot.loki_slices:
        parts.append(f"[loki.{slc.stream}]  ({len(slc.lines)} lines, truncated: {slc.truncated})")
        rendered = "\n".join(slc.lines[:_RENDER_LINE_CAP])
        if len(slc.lines) > _RENDER_LINE_CAP:
            rendered += f"\n... [{len(slc.lines) - _RENDER_LINE_CAP} more lines elided]"
        parts.append(rendered)

    parts.append("")
    parts.append("=== SSH artifacts ===")
    if not snapshot.ssh_artifacts:
        parts.append("(empty)")
    for art in snapshot.ssh_artifacts:
        parts.append(f"[ssh.{art.filename}] ({art.size_bytes} bytes)")
        contents = art.contents or ""
        parts.append(contents[:8000])
        if len(contents) > 8000:
            parts.append("... [truncated]")

    parts.append("")
    parts.append("=== Collection errors ===")
    parts.append(f"loki: {snapshot.loki_error or 'ok'}")
    parts.append(f"ssh:  {snapshot.ssh_error or 'ok'}")

    rendered = "\n".join(parts)
    if len(rendered) > _RENDER_BYTE_CAP:
        rendered = rendered[:_RENDER_BYTE_CAP] + "\n... [snapshot truncated to fit prompt budget]"
    return rendered


__all__ = [
    "MANIFEST",
    "JiraTriageHandler",
    "PauseGuard",
]
