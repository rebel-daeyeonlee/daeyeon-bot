"""PR review-comment auto-fix handler (feature 004).

Consumes `gh.pr_feedback` (auto path) and `pr.autofix.manual` (CLI path), and
is the daemon's ONLY handler that writes to a git remote. Everything about its
shape follows from that one fact.

Stage order — each stage is a gate, and the first one that trips wins:

    (a) PAUSE guard
    (b) re-entry guard from `pr_autofix_audit` (see `idempotent=False` below)
    (c) repo allowlist (auto path only; a manual fire is its own authorization)
    (d) `gh.pr_get` → HARD author gate: the bot pushes only to PRs the operator
        authored. A PR by anyone else is skipped before any other work.
    (e) closed / merged skip
    (f) `max_rounds` brake, announced once on the PR
    (g) collect feedback across the three comment surfaces, minus the ledger
    (h) TRIAGE — Claude judges each comment; retry once on schema failure
    (i) no `accepted` → reply to everything, ledger, done. The common outcome
        for a PR whose review comments were noise.
    (j) workspace clone + checkout of the live head SHA
    (k) FIX — a tool-enabled Claude agent edits the working tree
    (l) diff gates: empty / protected paths / size budget
    (m) verify command (per-repo); failure means NO push, and the comments stay
        pending so the next round retries them
    (n) commit + push
    (o) one reply per comment — accepted AND rejected alike — then ledger rows

`idempotent=False` is deliberate and load-bearing. `git push` cannot be
replayed, so an `interrupted` row must reach `dead_letter` for the operator
rather than being re-claimed (`infra/outbox.recover_interrupted_rows`). The
`in_progress` audit row is the second half of that guarantee: it distinguishes
"died before touching the remote" from "died mid-push".

Ledger rows are written only AFTER a comment has been answered on GitHub. A
crash before that leaves the comment pending, so the next poll retries it —
losing feedback silently is the worse failure.
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

import structlog
from pydantic import ValidationError as PydanticValidationError

from daeyeon_bot.app.config import PrAutofixHandlerEntry
from daeyeon_bot.core.errors import PermanentError, TransientError, ValidationError
from daeyeon_bot.core.events import Event
from daeyeon_bot.core.llm_json import extract_json_object
from daeyeon_bot.core.manifest import HandlerManifest
from daeyeon_bot.core.persona import Persona
from daeyeon_bot.core.pr_autofix.types import (
    FeedbackComment,
    FixOutcome,
    TriageDecision,
    VerifyOutcome,
    WorkspaceDiff,
)
from daeyeon_bot.core.protocols import HandlerContext
from daeyeon_bot.core.results import Ack, DeadLetter, HandlerResult
from daeyeon_bot.handlers.pr_autofix_comments import build_feedback
from daeyeon_bot.handlers.pr_autofix_prompt import (
    build_fix_system_prompt,
    build_triage_system_prompt,
    parse_verify_command,
    render_fix_message,
    render_triage_message,
)
from daeyeon_bot.handlers.pr_autofix_reply import (
    render_aggregate_comment,
    render_decision_reply,
    render_max_rounds_comment,
    render_verify_failed_comment,
)
from daeyeon_bot.handlers.pr_autofix_schemas import TriageOutput
from daeyeon_bot.infra import pr_autofix_audit, pr_autofix_ledger, pr_review_audit
from daeyeon_bot.infra.git_workspace import GitWorkspace
from daeyeon_bot.infra.persona_loader import PersonaLoader

_log = structlog.get_logger(__name__)

MANIFEST = HandlerManifest(
    name="pr_autofix",
    # NOT idempotent: `git push` has no undo. See the module docstring.
    idempotent=False,
    dedup_ttl=timedelta(days=1),
    # Serializes autofix runs against each other at the dispatcher level; two
    # concurrent agents editing sibling clones of the same repo would race on
    # the workspace directory.
    side_effect_key="pr_autofix",
    concurrency=1,
    accepts=("gh.pr_feedback", "pr.autofix.manual"),
)


@runtime_checkable
class _GhClient(Protocol):
    """The subset of `infra.gh_cli.GhCli` this handler needs."""

    async def pr_get(self, repo: str, pr_number: int) -> dict[str, Any]: ...
    async def pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    async def list_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    async def list_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    async def list_issue_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    async def reply_to_review_comment(
        self, repo: str, pr_number: int, comment_id: int, body: str
    ) -> dict[str, Any]: ...
    async def post_issue_comment(self, repo: str, pr_number: int, body: str) -> dict[str, Any]: ...


PauseGuard = Callable[[], Awaitable[None]]
# Builds a tool-enabled Claude session rooted at a workspace clone.
AgentSessionFactory = Callable[..., Any]


async def _no_pause() -> None:
    return None


@dataclass(frozen=True, slots=True)
class _Parsed:
    repo: str
    pr_number: int
    head_sha: str
    round: int
    comment_ids: tuple[int, ...]
    is_manual: bool


@dataclass(frozen=True, slots=True)
class _PrContext:
    """Live PR facts, re-read from GitHub rather than trusted from the payload."""

    repo: str
    pr_number: int
    head_sha: str
    head_repo: str
    head_ref: str
    title: str
    body: str
    author_login: str


@dataclass(slots=True)
class PrAutofixHandler:
    """Consumes `gh.pr_feedback` and `pr.autofix.manual` events."""

    manifest: HandlerManifest
    gh: _GhClient
    persona_loader: PersonaLoader
    config: PrAutofixHandlerEntry
    github_username: str
    db: Any  # aiosqlite.Connection
    agent_session_factory: AgentSessionFactory
    project_root: Path | None = None
    pause_guard: PauseGuard = _no_pause
    # Test seam. Production leaves this None and `_build_workspace` constructs a
    # real `GitWorkspace` from config; tests inject one already pointed at a
    # local bare repo so the pipeline can be exercised without a GitHub remote.
    workspace_factory: Callable[[str], GitWorkspace] | None = None

    async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
        budget = float(self.config.timeout_seconds)
        try:
            return await asyncio.wait_for(self._handle_inner(event, ctx), timeout=budget)
        except TimeoutError as exc:
            # First timeout → Retry (dispatcher maps TransientError). A second
            # timeout promotes to DeadLetter through the retry ladder. Nothing
            # has been pushed at this point unless the audit row says otherwise,
            # and the re-entry guard reads that on the next attempt.
            raise TransientError(
                f"pr_autofix exceeded {budget}s budget (asyncio.wait_for)"
            ) from exc

    # ── pipeline ──────────────────────────────────────────────────────────

    async def _handle_inner(  # noqa: PLR0911 — one early return per documented gate
        self, event: Event, ctx: HandlerContext
    ) -> HandlerResult:
        await self.pause_guard()
        parsed = _parse_payload(event)
        now = ctx.clock.now() if hasattr(ctx, "clock") else datetime.now(tz=UTC)

        guard = await self._reentry_guard(event, parsed)
        if guard is not None:
            return guard

        if not parsed.is_manual and not _is_repo_allowed(parsed.repo, self.config.allowed_repos):
            return await self._skip(
                event,
                parsed,
                status="skipped_disallowed_repo",
                now=now,
                error=f"repo={parsed.repo!r} not in allowed_repos={self.config.allowed_repos!r}",
            )

        pr_raw = await self.gh.pr_get(parsed.repo, parsed.pr_number)
        pr = _read_pr(parsed, pr_raw)

        # THE boundary the operator chose: never push to a branch that is not
        # theirs. Checked against the live payload, not the event, so a PR that
        # changed hands between emit and dispatch is still caught.
        if pr.author_login.lower() != self.github_username.lower():
            return await self._skip(
                event,
                parsed,
                status="skipped_not_author",
                now=now,
                error=f"PR author {pr.author_login!r} != operator {self.github_username!r}",
            )
        if _is_closed(pr_raw):
            return await self._skip(event, parsed, status="skipped_closed", now=now)

        if await self._max_rounds_tripped(event, parsed, now):
            return Ack()

        targets = await self._pending_comments(pr)
        if not targets:
            return await self._skip(event, parsed, status="skipped_nothing_pending", now=now)

        persona = self.persona_loader.load(
            self.config.persona_skill or "", min_chars=self.config.min_persona_chars
        )

        decisions = await self._triage(ctx, persona=persona, pr=pr, targets=targets)
        by_id = {c.comment_id: c for c in targets}
        accepted = [(d, by_id[d.comment_id]) for d in decisions if d.verdict == "accepted"]

        if not accepted:
            return await self._settle_without_push(
                event, parsed, pr, decisions, by_id, status="all_rejected", now=now
            )

        return await self._fix_and_push(
            event=event,
            parsed=parsed,
            pr=pr,
            persona=persona,
            decisions=decisions,
            by_id=by_id,
            accepted=accepted,
            now=now,
        )

    # ── gates ─────────────────────────────────────────────────────────────

    async def _reentry_guard(self, event: Event, parsed: _Parsed) -> HandlerResult | None:
        """Decide whether a second attempt at this event may touch GitHub.

        `pushed` means the work landed — Ack without redoing it. `in_progress`
        means we died between "about to commit" and "pushed", where a blind
        retry risks a duplicate commit on the branch; that is an operator
        decision, not one this handler may make on its own.
        """
        prior = await pr_autofix_audit.find_by_event(self.db, event.id)
        if prior is None:
            return None
        if prior.status == "pushed":
            _log.info(
                "pr_autofix.already_pushed",
                repo=parsed.repo,
                pr_number=parsed.pr_number,
                commit_sha=prior.commit_sha,
            )
            return Ack()
        if prior.status == "in_progress":
            return DeadLetter(
                f"pr_autofix interrupted mid-push for {parsed.repo}#{parsed.pr_number}"
                f" (audit id={prior.id}); inspect the branch before replaying"
            )
        return None

    async def _max_rounds_tripped(self, event: Event, parsed: _Parsed, now: datetime) -> bool:
        """True when this PR has spent its autofix budget.

        `max_rounds = 0` disables the brake. The stand-down notice is posted
        only on the first trip; a repeat would spam the PR every poll for as
        long as reviewers keep commenting.
        """
        limit = self.config.max_rounds
        if limit <= 0 or parsed.round <= limit:
            return False
        if not await self._already_announced_max_rounds(parsed):
            await self.gh.post_issue_comment(
                parsed.repo, parsed.pr_number, render_max_rounds_comment(rounds=limit)
            )
        await self._skip(event, parsed, status="skipped_max_rounds", now=now)
        _log.warning(
            "pr_autofix.max_rounds",
            repo=parsed.repo,
            pr_number=parsed.pr_number,
            round=parsed.round,
            limit=limit,
        )
        return True

    async def _already_announced_max_rounds(self, parsed: _Parsed) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM pr_autofix_audit"
            " WHERE repo = ? AND pr_number = ? AND status = 'skipped_max_rounds' LIMIT 1",
            (parsed.repo, parsed.pr_number),
        ) as cur:
            return await cur.fetchone() is not None

    async def _pending_comments(self, pr: _PrContext) -> list[FeedbackComment]:
        """Live feedback minus everything already answered.

        Recomputed here rather than trusting `event.payload.comment_ids`:
        comments that arrived between the trigger's emit and this dispatch get
        folded into the SAME round instead of costing another one, and a
        comment deleted in the meantime drops out.
        """
        review_comments = await self.gh.list_review_comments(pr.repo, pr.pr_number)
        reviews = await self.gh.list_reviews(pr.repo, pr.pr_number)
        issue_comments = await self.gh.list_issue_comments(pr.repo, pr.pr_number)
        feedback = build_feedback(
            review_comments=review_comments,
            reviews=reviews,
            issue_comments=issue_comments,
            operator_login=self.github_username,
            allow_globs=self.config.comment_authors,
            ignore_globs=self.config.ignored_authors,
            self_comment_markers=self.config.self_comment_markers,
            own_review_ids=await pr_review_audit.posted_review_ids(self.db, pr.repo, pr.pr_number),
        )
        handled = await pr_autofix_ledger.handled_comment_ids(
            self.db, repo=pr.repo, pr_number=pr.pr_number
        )
        return [c for c in feedback if c.comment_id not in handled]

    # ── stage: triage ─────────────────────────────────────────────────────

    async def _triage(
        self,
        ctx: HandlerContext,
        *,
        persona: Persona,
        pr: _PrContext,
        targets: list[FeedbackComment],
    ) -> list[TriageDecision]:
        """Ask Claude to judge each comment. One retry on schema failure.

        A decision for an id we did not ask about is dropped rather than
        treated as a hard failure — the model occasionally echoes an id from a
        quoted comment body, and discarding it is cheaper than a whole retry.
        An id we DID ask about but got no answer for is filled in as `deferred`
        so it still gets a reply and leaves the pending set.
        """
        files = await self.gh.pr_files(pr.repo, pr.pr_number)
        system = build_triage_system_prompt(persona.body)
        message = render_triage_message(
            repo=pr.repo,
            pr_number=pr.pr_number,
            title=pr.title,
            body=pr.body,
            head_sha=pr.head_sha,
            files=cast("list[dict[str, object]]", files),
            comments=targets,
        )

        last_error: str = ""
        for attempt in (1, 2):
            await self.pause_guard()
            session_factory = ctx.claude_session_factory
            async with cast("Any", session_factory()) as session:
                raw = await session.query(message, system=system)
            try:
                parsed = TriageOutput.model_validate_json(extract_json_object(raw))
            except (PydanticValidationError, ValueError) as exc:
                last_error = str(exc)
                _log.warning(
                    "pr_autofix.triage_invalid",
                    repo=pr.repo,
                    pr_number=pr.pr_number,
                    attempt=attempt,
                    error=last_error[:500],
                )
                continue
            return _reconcile_decisions(parsed, targets)
        raise ValidationError(f"pr_autofix triage output invalid twice: {last_error[:500]}")

    # ── stage: fix + push ─────────────────────────────────────────────────

    async def _fix_and_push(
        self,
        *,
        event: Event,
        parsed: _Parsed,
        pr: _PrContext,
        persona: Persona,
        decisions: list[TriageDecision],
        by_id: dict[int, FeedbackComment],
        accepted: list[tuple[TriageDecision, FeedbackComment]],
        now: datetime,
    ) -> HandlerResult:
        audit_id = await self._open_run(event, parsed, pr, persona, decisions, now)

        workspace = self._build_workspace(pr.repo)
        await workspace.ensure_clone()
        await workspace.checkout_pr(pr_number=pr.pr_number, head_sha=pr.head_sha)

        await self.pause_guard()
        summary = await self._run_fix_agent(workspace, persona=persona, pr=pr, accepted=accepted)
        diff = await workspace.diff()
        outcome = FixOutcome(summary=summary, diff=diff)

        if diff.is_empty:
            # The agent judged every accepted item unimplementable, or its
            # edits were no-ops. Reporting this honestly matters: claiming a
            # fix that is not on the branch would stop reviewers from checking.
            _log.info("pr_autofix.no_changes", repo=pr.repo, pr_number=pr.pr_number)
            return await self._settle_after_fix(
                event, parsed, pr, decisions, by_id, audit_id, outcome, "no_changes", now
            )

        violated = _protected_violations(diff.changed_files, self.config.protected_paths)
        if violated:
            await workspace.revert_working_tree()
            return await self._settle_after_fix(
                event,
                parsed,
                pr,
                decisions,
                by_id,
                audit_id,
                FixOutcome(summary=summary, diff=diff),
                "skipped_protected_path",
                now,
                error=f"agent modified protected paths: {', '.join(violated)}",
            )

        if (
            len(diff.changed_files) > self.config.max_changed_files
            or diff.total_lines > self.config.max_changed_lines
        ):
            await workspace.revert_working_tree()
            return await self._settle_after_fix(
                event,
                parsed,
                pr,
                decisions,
                by_id,
                audit_id,
                outcome,
                "skipped_diff_too_large",
                now,
                error=(
                    f"diff {len(diff.changed_files)} files / {diff.total_lines} lines"
                    f" exceeds budget {self.config.max_changed_files}/"
                    f"{self.config.max_changed_lines}"
                ),
            )

        verify = await self._verify(workspace, pr.repo, agent_reply=summary)
        if verify is not None and not verify.passed:
            return await self._handle_verify_failure(
                workspace, event, parsed, pr, audit_id, diff, verify, len(accepted), now
            )

        outcome = FixOutcome(summary=summary, diff=diff, verify=verify)
        commit_sha = await workspace.commit_all(
            _commit_message(self.config.commit_message_prefix, accepted, pr)
        )
        dry_run = not self.config.push_enabled
        if dry_run:
            # The commit lives only in the workspace clone, which the next
            # round resets. Nothing below may present it as shipped.
            _log.warning(
                "pr_autofix.push_disabled",
                repo=pr.repo,
                pr_number=pr.pr_number,
                local_commit_sha=commit_sha,
            )
        else:
            await workspace.push(head_repo=pr.head_repo, head_ref=pr.head_ref)

        # `shipped_sha` is what the reply and the ledger may cite: the SHA a
        # reviewer can actually open. In a dry run there is no such SHA.
        shipped_sha = None if dry_run else commit_sha
        outcome = FixOutcome(summary=summary, diff=diff, commit_sha=shipped_sha, verify=verify)
        await self._reply_all(
            pr, decisions, by_id, outcome=outcome, commit_sha=shipped_sha, dry_run=dry_run
        )
        await pr_autofix_audit.finish_run(
            self.db,
            audit_id,
            status="dry_run" if dry_run else "pushed",
            accepted_count=len(accepted),
            rejected_count=_count(decisions, "rejected"),
            deferred_count=_count(decisions, "deferred"),
            # The local SHA is kept for forensics even in a dry run — it is the
            # only handle on what the agent actually produced — but `pushed_at`
            # stays NULL, which is what distinguishes shipped from not.
            commit_sha=commit_sha,
            pushed_at=None if dry_run else now,
            changed_files=len(diff.changed_files),
            changed_lines=diff.total_lines,
            verify_command=verify.command if verify else None,
            verify_exit_code=verify.exit_code if verify else None,
        )
        await self._write_ledger(
            event, pr, decisions, by_id, commit_sha=shipped_sha, now=now, dry_run=dry_run
        )
        await self.db.commit()
        _log.info(
            "pr_autofix.dry_run" if dry_run else "pr_autofix.pushed",
            repo=pr.repo,
            pr_number=pr.pr_number,
            commit_sha=commit_sha,
            accepted=len(accepted),
            changed_files=len(diff.changed_files),
        )
        return Ack()

    async def _run_fix_agent(
        self,
        workspace: GitWorkspace,
        *,
        persona: Persona,
        pr: _PrContext,
        accepted: list[tuple[TriageDecision, FeedbackComment]],
    ) -> str:
        system = build_fix_system_prompt(persona.body, protected_paths=self.config.protected_paths)
        message = render_fix_message(
            repo=pr.repo,
            pr_number=pr.pr_number,
            head_sha=pr.head_sha,
            accepted=accepted,
        )
        async with self.agent_session_factory(cwd=workspace.path) as session:
            return await session.query(message, system=system)

    async def _verify(
        self, workspace: GitWorkspace, repo: str, *, agent_reply: str
    ) -> VerifyOutcome | None:
        """Run the pre-push check, or None when there is nothing to run.

        Resolution order:
          1. `[handlers.pr_autofix.verify_commands]` for this repo — an operator
             override, normally empty.
          2. The `VERIFY:` line the fix agent reported. The agent picks it by
             reading the repository, so nothing in config goes stale when a repo
             changes its CI.
          3. Nothing — push and let the PR's own CI be the judge. That is the
             real gate anyway; this stage is a fast first line of defence, not a
             reimplementation of the repo's pipeline.

        Either way the handler runs the command and reads the exit code itself,
        so "did it pass" never rests on the model's own account of it.
        """
        override = self.config.verify_command_for(repo)
        command = override or parse_verify_command(agent_reply) or ""
        if not command:
            _log.info("pr_autofix.verify_skipped", repo=repo, reason="no command available")
            return None
        outcome = await workspace.run_verify(
            command, timeout_s=float(self.config.verify_timeout_seconds)
        )
        _log.info(
            "pr_autofix.verify",
            repo=repo,
            command=command,
            source="config_override" if override else "agent",
            exit_code=outcome.exit_code,
        )
        return outcome

    async def _handle_verify_failure(
        self,
        workspace: GitWorkspace,
        event: Event,
        parsed: _Parsed,
        pr: _PrContext,
        audit_id: int,
        diff: WorkspaceDiff,
        verify: VerifyOutcome,
        accepted_count: int,
        now: datetime,
    ) -> HandlerResult:
        """Verification failed: revert, announce, push nothing.

        No ledger rows are written. The comments stay pending, so the next poll
        emits a fresh event and the fix is attempted again — possibly against a
        head that has moved on. That is the intended behavior: a failed verify
        is a "not yet", not a verdict on the feedback.
        """
        del event, parsed
        await workspace.revert_working_tree()
        await self.gh.post_issue_comment(
            pr.repo,
            pr.pr_number,
            render_verify_failed_comment(
                repo=pr.repo, verify=verify, accepted_count=accepted_count
            ),
        )
        await pr_autofix_audit.finish_run(
            self.db,
            audit_id,
            status="verify_failed",
            accepted_count=accepted_count,
            changed_files=len(diff.changed_files),
            changed_lines=diff.total_lines,
            verify_command=verify.command,
            verify_exit_code=verify.exit_code,
            error=verify.output_tail[-1000:],
        )
        await self.db.commit()
        _log.warning(
            "pr_autofix.verify_failed",
            repo=pr.repo,
            pr_number=pr.pr_number,
            command=verify.command,
            exit_code=verify.exit_code,
        )
        return Ack()

    # ── settle helpers ────────────────────────────────────────────────────

    async def _open_run(
        self,
        event: Event,
        parsed: _Parsed,
        pr: _PrContext,
        persona: Persona,
        decisions: list[TriageDecision],
        now: datetime,
    ) -> int:
        audit_id = await pr_autofix_audit.insert_audit(
            self.db,
            event_id=event.id,
            repo=pr.repo,
            pr_number=pr.pr_number,
            head_sha=pr.head_sha,
            round=parsed.round,
            status="in_progress",
            created_at=now,
            accepted_count=_count(decisions, "accepted"),
            rejected_count=_count(decisions, "rejected"),
            deferred_count=_count(decisions, "deferred"),
            persona_skill=persona.name,
            persona_mtime_ns=persona.mtime_ns,
        )
        await self.db.commit()
        return audit_id

    async def _settle_without_push(
        self,
        event: Event,
        parsed: _Parsed,
        pr: _PrContext,
        decisions: list[TriageDecision],
        by_id: dict[int, FeedbackComment],
        *,
        status: pr_autofix_audit.AutofixStatus,
        now: datetime,
    ) -> HandlerResult:
        """Answer everything and record the run, without ever touching git."""
        await self._reply_all(pr, decisions, by_id, outcome=None, commit_sha=None)
        await pr_autofix_audit.insert_audit(
            self.db,
            event_id=event.id,
            repo=pr.repo,
            pr_number=pr.pr_number,
            head_sha=pr.head_sha,
            round=parsed.round,
            status=status,
            created_at=now,
            accepted_count=_count(decisions, "accepted"),
            rejected_count=_count(decisions, "rejected"),
            deferred_count=_count(decisions, "deferred"),
        )
        await self._write_ledger(event, pr, decisions, by_id, commit_sha=None, now=now)
        await self.db.commit()
        _log.info(
            "pr_autofix.settled",
            repo=pr.repo,
            pr_number=pr.pr_number,
            status=status,
            decisions=len(decisions),
        )
        return Ack()

    async def _settle_after_fix(
        self,
        event: Event,
        parsed: _Parsed,
        pr: _PrContext,
        decisions: list[TriageDecision],
        by_id: dict[int, FeedbackComment],
        audit_id: int,
        outcome: FixOutcome,
        status: pr_autofix_audit.AutofixStatus,
        now: datetime,
        error: str | None = None,
    ) -> HandlerResult:
        """Close an `in_progress` run that produced no pushable commit."""
        del parsed
        await self._reply_all(pr, decisions, by_id, outcome=outcome, commit_sha=None)
        await pr_autofix_audit.finish_run(
            self.db,
            audit_id,
            status=status,
            accepted_count=_count(decisions, "accepted"),
            rejected_count=_count(decisions, "rejected"),
            deferred_count=_count(decisions, "deferred"),
            changed_files=len(outcome.diff.changed_files),
            changed_lines=outcome.diff.total_lines,
            error=error,
        )
        await self._write_ledger(event, pr, decisions, by_id, commit_sha=None, now=now)
        await self.db.commit()
        _log.info(
            "pr_autofix.settled_after_fix",
            repo=pr.repo,
            pr_number=pr.pr_number,
            status=status,
            error=error,
        )
        return Ack()

    async def _skip(
        self,
        event: Event,
        parsed: _Parsed,
        *,
        status: pr_autofix_audit.AutofixStatus,
        now: datetime,
        error: str | None = None,
    ) -> HandlerResult:
        await pr_autofix_audit.insert_audit(
            self.db,
            event_id=event.id,
            repo=parsed.repo,
            pr_number=parsed.pr_number,
            head_sha=parsed.head_sha,
            round=parsed.round,
            status=status,
            created_at=now,
            error=error,
        )
        await self.db.commit()
        _log.info(
            "pr_autofix.skipped",
            repo=parsed.repo,
            pr_number=parsed.pr_number,
            status=status,
            error=error,
        )
        return Ack()

    # ── replies + ledger ──────────────────────────────────────────────────

    async def _reply_all(
        self,
        pr: _PrContext,
        decisions: list[TriageDecision],
        by_id: dict[int, FeedbackComment],
        *,
        outcome: FixOutcome | None,
        commit_sha: str | None,
        dry_run: bool = False,
    ) -> None:
        """Answer every triaged comment — accepted, rejected and deferred alike.

        Inline comments get an in-thread reply each; PR-level ones are folded
        into a single conversation comment so the PR is not buried in bot posts.
        A failed reply is logged and swallowed: the ledger row is still written,
        because a comment we decided on but could not answer must not spin
        forever in the pending set.
        """
        aggregate: list[tuple[TriageDecision, FeedbackComment]] = []
        for decision in decisions:
            comment = by_id.get(decision.comment_id)
            if comment is None:
                continue
            if comment.kind != "review_comment":
                aggregate.append((decision, comment))
                continue
            body = render_decision_reply(
                decision,
                repo=pr.repo,
                outcome=outcome,
                commit_sha=commit_sha,
                dry_run=dry_run,
            )
            target = comment.reply_target_id or comment.comment_id
            try:
                await self.gh.reply_to_review_comment(pr.repo, pr.pr_number, target, body)
            except (PermanentError, TransientError) as exc:
                _log.warning(
                    "pr_autofix.reply_failed",
                    repo=pr.repo,
                    pr_number=pr.pr_number,
                    comment_id=comment.comment_id,
                    error=str(exc),
                )

        if not aggregate:
            return
        body = render_aggregate_comment(
            aggregate,
            repo=pr.repo,
            outcome=outcome,
            commit_sha=commit_sha,
            dry_run=dry_run,
        )
        try:
            await self.gh.post_issue_comment(pr.repo, pr.pr_number, body)
        except (PermanentError, TransientError) as exc:
            _log.warning(
                "pr_autofix.aggregate_reply_failed",
                repo=pr.repo,
                pr_number=pr.pr_number,
                error=str(exc),
            )

    async def _write_ledger(
        self,
        event: Event,
        pr: _PrContext,
        decisions: list[TriageDecision],
        by_id: dict[int, FeedbackComment],
        *,
        commit_sha: str | None,
        now: datetime,
        dry_run: bool = False,
    ) -> None:
        """Mark every decided comment handled. This is what ends the loop.

        A dry-run `accepted` is recorded as `deferred`, never `accepted`. The
        distinction is not cosmetic: an `accepted` row means "done", so turning
        `push_enabled` on later would never revisit it and the fix — which only
        ever existed in a wiped workspace — would be silently lost. `deferred`
        says what is true: a human still owns this one. The reply tells the
        reviewer how to bring it back (comment again → new id → pending again).
        """
        for decision in decisions:
            comment = by_id.get(decision.comment_id)
            if comment is None:
                continue
            verdict = decision.verdict
            if verdict == "accepted" and dry_run:
                verdict = "deferred"  # type: ignore[assignment]
            # An `accepted` comment with no commit was NOT fixed; recording it
            # as accepted would leave a false trail in the ledger.
            elif verdict == "accepted" and commit_sha is None:
                verdict = "failed"  # type: ignore[assignment]
            await pr_autofix_ledger.record_decision(
                self.db,
                repo=pr.repo,
                pr_number=pr.pr_number,
                comment_id=comment.comment_id,
                comment_kind=comment.kind,
                author=comment.author,
                verdict=verdict,
                created_at=now,
                event_id=event.id,
                reason=(
                    "dry run (push_enabled=false) — 수정안만 만들고 브랜치에는 반영하지"
                    " 않음. " + decision.reasoning
                    if dry_run and decision.verdict == "accepted"
                    else decision.reasoning
                )[:2000],
                commit_sha=commit_sha if decision.verdict == "accepted" else None,
                replied=True,
            )

    # ── misc ──────────────────────────────────────────────────────────────

    def _build_workspace(self, repo: str) -> GitWorkspace:
        if self.workspace_factory is not None:
            return self.workspace_factory(repo)
        root = Path(self.config.workspace_root).expanduser()
        if not root.is_absolute() and self.project_root is not None:
            root = self.project_root / root
        return GitWorkspace(
            repo=repo,
            workspace_root=root,
            project_root=self.project_root,
            allow_external=self.config.allow_external_workspace,
            git_timeout_s=float(self.config.git_timeout_seconds),
            author_name=self.config.git_author_name,
            author_email=self.config.git_author_email,
        )


# ── module helpers ────────────────────────────────────────────────────────


def _parse_payload(event: Event) -> _Parsed:
    payload = event.payload
    repo = str(payload.get("repo", ""))
    pr_number = payload.get("pr_number")
    if not repo or "/" not in repo or not isinstance(pr_number, int):
        raise PermanentError(f"pr_autofix: malformed payload {payload!r}")
    raw_ids = payload.get("comment_ids")
    ids = tuple(i for i in raw_ids if isinstance(i, int)) if isinstance(raw_ids, list) else ()
    round_raw = payload.get("round")
    return _Parsed(
        repo=repo,
        pr_number=pr_number,
        head_sha=str(payload.get("head_sha", "")),
        round=round_raw if isinstance(round_raw, int) else 1,
        comment_ids=ids,
        is_manual=event.type == "pr.autofix.manual",
    )


def _subobject(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """`raw[key]` when it is an object, else `{}`. GitHub nulls `head.repo` for
    a PR whose fork was deleted, so every nested read has to tolerate None."""
    value = raw.get(key)
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _read_pr(parsed: _Parsed, raw: dict[str, Any]) -> _PrContext:
    """Project the `GET /pulls/{n}` payload onto what the pipeline needs."""
    head = _subobject(raw, "head")
    head_repo_raw = _subobject(head, "repo")
    user = _subobject(raw, "user")
    head_sha = str(head.get("sha") or parsed.head_sha)
    if not head_sha:
        raise PermanentError(f"pr_autofix: no head SHA for {parsed.repo}#{parsed.pr_number}")
    head_repo = str(head_repo_raw.get("full_name") or parsed.repo)
    head_ref = str(head.get("ref") or "")
    if not head_ref:
        raise PermanentError(f"pr_autofix: no head ref for {parsed.repo}#{parsed.pr_number}")
    return _PrContext(
        repo=parsed.repo,
        pr_number=parsed.pr_number,
        head_sha=head_sha,
        head_repo=head_repo,
        head_ref=head_ref,
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
        author_login=str(user.get("login") or ""),
    )


def _is_closed(raw: dict[str, Any]) -> bool:
    return str(raw.get("state", "")).lower() != "open" or bool(raw.get("merged_at"))


def _is_repo_allowed(repo: str, patterns: list[str]) -> bool:
    """Empty allowlist = no repo filter; the author gate is then the boundary."""
    if not patterns:
        return True
    return any(fnmatch.fnmatch(repo, p) for p in patterns)


def _protected_violations(changed: tuple[str, ...], patterns: list[str]) -> list[str]:
    """Changed paths matching a protected glob.

    Checked against the RESULTING diff rather than the agent's intent, so a
    protected file edited by a `Bash` invocation is caught just the same as one
    edited with `Write`.
    """
    if not patterns:
        return []
    return [
        path
        for path in changed
        if any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(f"/{path}", p) for p in patterns)
    ]


def _count(decisions: list[TriageDecision], verdict: str) -> int:
    return sum(1 for d in decisions if d.verdict == verdict)


def _commit_message(
    prefix: str, accepted: list[tuple[TriageDecision, FeedbackComment]], pr: _PrContext
) -> str:
    """Commit message naming every comment the commit answers.

    The trailer is not decoration: it is what lets a human reading `git log`
    six weeks later find the review thread that motivated a change.
    """
    heads = [d.fix_instruction.strip().splitlines()[0] for d, _ in accepted if d.fix_instruction]
    subject = heads[0] if len(heads) == 1 else f"address {len(accepted)} review comments"
    subject = subject[:68]
    lines = [f"{prefix}: {subject}", ""]
    for decision, comment in accepted:
        lines.append(f"- [{comment.author}] {decision.fix_instruction.strip()[:200]}")
    lines.append("")
    lines.append(f"Addresses review comments on {pr.repo}#{pr.pr_number}:")
    lines.append(" " + ", ".join(f"#{d.comment_id}" for d, _ in accepted))
    lines.append("")
    lines.append("Co-Authored-By: daeyeon-bot <daeyeon-bot@users.noreply.github.com>")
    return "\n".join(lines)


def _reconcile_decisions(
    parsed: TriageOutput, targets: list[FeedbackComment]
) -> list[TriageDecision]:
    """Align Claude's decisions with the comments we actually asked about."""
    wanted = {c.comment_id for c in targets}
    by_id: dict[int, TriageDecision] = {}
    for out in parsed.decisions:
        if out.comment_id not in wanted:
            _log.warning("pr_autofix.triage_unknown_comment_id", comment_id=out.comment_id)
            continue
        by_id[out.comment_id] = TriageDecision(
            comment_id=out.comment_id,
            verdict=out.verdict,
            reasoning=out.reasoning.strip(),
            fix_instruction=out.fix_instruction.strip(),
            evidence=out.evidence.strip(),
        )
    for comment in targets:
        if comment.comment_id in by_id:
            continue
        # Unanswered by the model. Defer rather than drop: the comment still
        # gets a reply and still leaves the pending set, so the loop converges.
        _log.warning("pr_autofix.triage_missing_decision", comment_id=comment.comment_id)
        by_id[comment.comment_id] = TriageDecision(
            comment_id=comment.comment_id,
            verdict="deferred",
            reasoning=(
                "자동 판정이 이 코멘트에 대한 결론을 내지 못했습니다. 사람 확인이 필요합니다."
            ),
        )
    return [by_id[c.comment_id] for c in targets]


__all__ = ["MANIFEST", "PauseGuard", "PrAutofixHandler"]
