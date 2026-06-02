"""GitHub PR review handler (T024).

The single handler that consumes both `gh.review_requested` (auto path) and
`pr.review.manual` (CLI path). Stages, in the exact order required by
`data-model.md` §4:

    (a) load active persona (validation failure → DeadLetter)
    (b) gh.pr_get to refresh head SHA + author + requested reviewers
    (c) self-authored skip
    (d) withdrawn skip (no longer a requested reviewer; or PR closed)
    (e) gh.pr_files → size budget; if exceeded post the templated "too
        large" Summary then Ack + audit `skipped_too_large`
    (f) audit lookup for `(repo, pr, head_sha)`; if found and not force
        → Ack + audit `skipped_already_reviewed`
    (g) call Claude; validate with `ReviewOutput`; retry once on validate
        failure; second failure → DeadLetter
    (h) `_filter_anchors` folds out-of-hunk inline comments into the Summary
    (h.5) `_redact` over Summary + every comment body — any match raises
          PermanentError("redaction would alter posted content")
    (i) prepend supersede header when force-reviewing on top of a prior
        posted review
    (j) gh.post_review (event="COMMENT" — baked-in inside `gh_cli`)
    (k) record_supersede + insert_audit(status='posted') + Ack

`gh_cli` raises `AuthError` / `RateLimitError` / `TransientError` /
`PermanentError`; the dispatcher's exception mapping turns those into halt
/ Retry / DeadLetter without further translation in this module.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast, runtime_checkable

import structlog
from pydantic import ValidationError as PydanticValidationError

from daeyeon_bot.app.config import PrReviewHandlerEntry
from daeyeon_bot.core.errors import (
    PermanentError,
    ValidationError,
)
from daeyeon_bot.core.events import Event
from daeyeon_bot.core.manifest import HandlerManifest
from daeyeon_bot.core.pr_review.audit import AuditRow
from daeyeon_bot.core.pr_review.persona import Persona
from daeyeon_bot.core.protocols import HandlerContext
from daeyeon_bot.core.results import Ack, HandlerResult
from daeyeon_bot.handlers.pr_review_diff import (
    is_anchor_in_hunk,
    parse_hunk_ranges,
)
from daeyeon_bot.handlers.pr_review_lgtm import pick_lgtm_gif
from daeyeon_bot.handlers.pr_review_prompt import build_system_prompt
from daeyeon_bot.handlers.pr_review_render import inline_to_api, render_user_message
from daeyeon_bot.handlers.pr_review_schemas import InlineComment, ReviewOutput
from daeyeon_bot.infra.logging import RedactReason, redact_with_provenance
from daeyeon_bot.infra.pr_review_audit import (
    find_latest,
    insert_audit,
    record_supersede,
)
from daeyeon_bot.infra.pr_review_persona import PersonaLoader

_log = structlog.get_logger(__name__)

MANIFEST = HandlerManifest(
    name="pr_review",
    idempotent=True,
    dedup_ttl=timedelta(days=1),
    side_effect_key=None,
    concurrency=1,
    accepts=("gh.review_requested", "pr.review.manual"),
)

# Templated body when the size budget is exceeded (claude-review-output.md §5).
_TOO_LARGE_TEMPLATE = (
    "This PR is too large for an automated review at SHA `{head_sha}`.\n"
    "\n"
    "- Changed files: {n_files} (limit {max_files})\n"
    "- Changed lines: {n_lines} (limit {max_lines})\n"
    "\n"
    "Consider splitting the change into smaller PRs and re-requesting review."
)


@runtime_checkable
class _GhClient(Protocol):
    """The subset of `infra.gh_cli.GhCli` this handler needs."""

    async def pr_get(self, repo: str, pr_number: int) -> dict[str, Any]: ...
    async def pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    async def list_prior_reviews_with_comments(
        self,
        repo: str,
        pr_number: int,
        *,
        login: str,
        limit: int = ...,
    ) -> list[dict[str, Any]]: ...
    async def post_review(
        self,
        repo: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
        event: str = ...,
        login: str | None = None,
    ) -> dict[str, Any]: ...


# Async callable returning `None` when no PAUSE flag is up, raising QuotaError
# otherwise. The container wires this from `app.pause.is_paused`.
PauseGuard = Callable[[], Awaitable[None]]


async def _no_pause() -> None:
    """Default no-op pause guard used when the container hasn't wired one."""
    return None


@dataclass(slots=True)
class PrReviewHandler:
    """Consumes `gh.review_requested` and `pr.review.manual` events."""

    manifest: HandlerManifest
    gh: _GhClient
    persona_loader: PersonaLoader
    config: PrReviewHandlerEntry
    github_username: str
    db: Any  # aiosqlite.Connection — Any to avoid circular type stub on tests.
    pause_guard: PauseGuard = _no_pause

    async def handle(self, event: Event, ctx: HandlerContext) -> HandlerResult:
        # PAUSE check first so even the size-budget short-circuit honors it.
        await self.pause_guard()
        parsed = _parse_payload(event)
        now = ctx.clock.now()

        early = await self._gate_repo_allowlist(event, parsed, now)
        if early is not None:
            return early

        persona = await self._load_persona_or_audit(event, parsed, now)

        prep = await self._fetch_pr_metadata(event, parsed, persona, now)

        early = await self._gate_self_or_withdrawn(prep, now)
        if early is not None:
            return early

        sized = await self._fetch_files(prep)
        early = await self._gate_size_budget(sized, now)
        if early is not None:
            return early

        prior = await find_latest(self.db, sized.repo, sized.pr_number, sized.head_sha)
        already_posted = prior is not None and prior.status == "posted"
        if already_posted and not sized.force:
            return await self._record_already_reviewed(sized, prior, now)

        await self.pause_guard()
        # Best-effort prior-review fetch — when populated, the persona
        # produces Resolved / Still open / New buckets. Errors swallowed
        # inside the gh_cli wrapper return `[]`, so a flaky `gh api` call
        # never blocks a review.
        prior_reviews = await self.gh.list_prior_reviews_with_comments(
            sized.repo, sized.pr_number, login=self.github_username, limit=2
        )
        review = await self._call_claude_with_retry(
            ctx=ctx,
            system_prompt=build_system_prompt(sized.persona.body),
            user_message=render_user_message(
                repo=sized.repo,
                pr_number=sized.pr_number,
                title=str(sized.pr.get("title", "")),
                body=str(sized.pr.get("body") or ""),
                author_login=sized.author_login,
                head_sha=sized.head_sha,
                files=sized.files,
                prior_reviews=prior_reviews,
            ),
        )

        await self.pause_guard()
        return await self._post_review_and_audit(
            sized, review, prior=prior, already_posted=already_posted, now=now
        )

    # ── stage helpers ──────────────────────────────────────────────────────

    async def _gate_repo_allowlist(
        self, event: Event, parsed: _Parsed, now: datetime
    ) -> HandlerResult | None:
        # The allowlist guards the AUTO poller from fanning out to repos the
        # operator never intended. An explicit `dev fire-pr-review` is the
        # operator's own authorization for that specific PR, so it bypasses
        # the allowlist (they already have `gh` access to that repo). For auto
        # events the allowlist still gates before any persona load / `gh.pr_get`;
        # `force=True` does NOT bypass it (the trigger sets force on re-reviews).
        if parsed.is_manual or _is_repo_allowed(parsed.repo, self.config.allowed_repos):
            if parsed.is_manual and not _is_repo_allowed(parsed.repo, self.config.allowed_repos):
                _log.info(
                    "pr_review.allowlist_bypassed_manual",
                    repo=parsed.repo,
                    pr_number=parsed.pr_number,
                    head_sha=parsed.head_sha,
                )
            return None
        await self._write_audit(
            event_id=event.id,
            repo=parsed.repo,
            pr_number=parsed.pr_number,
            head_sha=parsed.head_sha,
            request_gen=str(parsed.request_gen),
            status="skipped_disallowed_repo",
            error=(f"repo={parsed.repo!r} not in allowed_repos={self.config.allowed_repos!r}"),
            created_at=now,
        )
        _log.info(
            "pr_review.skipped_disallowed_repo",
            repo=parsed.repo,
            pr_number=parsed.pr_number,
            head_sha=parsed.head_sha,
            allowed_repos=self.config.allowed_repos,
        )
        return Ack()

    async def _load_persona_or_audit(self, event: Event, parsed: _Parsed, now: datetime) -> Persona:
        skill_name = self.config.persona_skill or ""
        try:
            return self.persona_loader.load(skill_name, min_chars=self.config.min_persona_chars)
        except ValidationError as exc:
            # Persona load failed — record the *configured* skill name so the
            # operator can correlate the failure with which persona file was
            # active. `persona_mtime_ns` is unknown at this point.
            await self._write_audit(
                event_id=event.id,
                repo=parsed.repo,
                pr_number=parsed.pr_number,
                head_sha=parsed.head_sha,
                request_gen=str(parsed.request_gen),
                status="failed",
                persona_skill=skill_name or None,
                error=str(exc),
                created_at=now,
            )
            raise

    async def _fetch_pr_metadata(
        self, event: Event, parsed: _Parsed, persona: Persona, now: datetime
    ) -> _PrepState:
        pr = await self.gh.pr_get(parsed.repo, parsed.pr_number)
        head_sha = _read_head_sha(pr) or parsed.head_sha
        return _PrepState(
            event_id=event.id,
            repo=parsed.repo,
            pr_number=parsed.pr_number,
            request_gen=parsed.request_gen,
            force=parsed.force,
            is_manual=parsed.is_manual,
            head_sha=head_sha,
            persona=persona,
            pr=pr,
            author_login=_read_author(pr),
            requested_logins=_read_requested_logins(pr),
            pr_state=str(pr.get("state", "open")),
            audit_kwargs={
                "event_id": event.id,
                "repo": parsed.repo,
                "pr_number": parsed.pr_number,
                "head_sha": head_sha,
                "request_gen": str(parsed.request_gen),
                "persona_skill": persona.name,
                "persona_mtime_ns": persona.mtime_ns,
            },
            now=now,
        )

    async def _gate_self_or_withdrawn(
        self, prep: _PrepState, now: datetime
    ) -> HandlerResult | None:
        is_self = bool(prep.author_login) and prep.author_login == self.github_username
        # Skip own PRs unless `review_self` opts in — but an explicit manual
        # fire always reviews. The post stage forces a COMMENT event for
        # self-authored PRs (GitHub rejects self-APPROVE).
        if is_self and not self.config.review_self and not prep.is_manual:
            await self._write_audit(
                **prep.audit_kwargs,
                status="skipped_self_authored",
                error=f"author_login={prep.author_login!r} == github_username",
                created_at=now,
            )
            _log.info(
                "pr_review.skipped_self_authored",
                repo=prep.repo,
                pr_number=prep.pr_number,
                head_sha=prep.head_sha,
            )
            return Ack()
        # Manual fires and force re-runs honor the request even when the
        # operator is not (or no longer) a requested reviewer; auto runs
        # always require current membership.
        if prep.force or prep.is_manual:
            return None
        # Own PRs are never in their own requested-reviewers set, so the
        # reviewer-membership test can't apply — only PR closure withdraws them.
        if is_self:
            withdrawn = prep.pr_state != "open"
        else:
            withdrawn = prep.pr_state != "open" or self.github_username not in prep.requested_logins
        if not withdrawn:
            return None
        await self._write_audit(
            **prep.audit_kwargs,
            status="skipped_withdrawn",
            error=f"state={prep.pr_state!r}, requested={prep.requested_logins}",
            created_at=now,
        )
        _log.info(
            "pr_review.skipped_withdrawn",
            repo=prep.repo,
            pr_number=prep.pr_number,
            head_sha=prep.head_sha,
            state=prep.pr_state,
        )
        return Ack()

    async def _fetch_files(self, prep: _PrepState) -> _SizedState:
        files_raw = await self.gh.pr_files(prep.repo, prep.pr_number)
        files = [_normalize_file(f) for f in files_raw]
        n_lines = sum(int(f.get("additions", 0)) + int(f.get("deletions", 0)) for f in files)
        return _SizedState.from_prep(prep, files=files, n_files=len(files), n_lines=n_lines)

    async def _gate_size_budget(self, sized: _SizedState, now: datetime) -> HandlerResult | None:
        budget = self.config.size_budget
        if sized.n_files <= budget.max_files and sized.n_lines <= budget.max_lines:
            return None
        await self.pause_guard()
        summary = _TOO_LARGE_TEMPLATE.format(
            head_sha=sized.head_sha,
            n_files=sized.n_files,
            max_files=budget.max_files,
            n_lines=sized.n_lines,
            max_lines=budget.max_lines,
        )
        posted = await self.gh.post_review(
            sized.repo,
            sized.pr_number,
            commit_id=sized.head_sha,
            body=summary,
            comments=[],
            login=self.github_username,
        )
        await self._write_audit(
            **sized.audit_kwargs,
            status="skipped_too_large",
            review_id=_read_review_id(posted),
            submitted_at=_read_submitted_at(posted),
            summary_chars=len(summary),
            inline_comment_count=0,
            created_at=now,
        )
        _log.info(
            "pr_review.skipped_too_large",
            repo=sized.repo,
            pr_number=sized.pr_number,
            n_files=sized.n_files,
            n_lines=sized.n_lines,
        )
        return Ack()

    async def _record_already_reviewed(
        self, sized: _SizedState, prior: AuditRow | None, now: datetime
    ) -> HandlerResult:
        await self._write_audit(
            **sized.audit_kwargs,
            status="skipped_already_reviewed",
            error=(f"prior_review_id={prior.review_id}" if prior is not None else "prior=unknown"),
            created_at=now,
        )
        _log.info(
            "pr_review.skipped_already_reviewed",
            repo=sized.repo,
            pr_number=sized.pr_number,
            head_sha=sized.head_sha,
            prior_review_id=prior.review_id if prior else None,
        )
        return Ack()

    async def _post_review_and_audit(
        self,
        sized: _SizedState,
        review: ReviewOutput,
        *,
        prior: AuditRow | None,
        already_posted: bool,
        now: datetime,
    ) -> HandlerResult:
        # (h) filter, (h.5) redact, (i) supersede header, (j) post, (k) audit.
        kept, folded = _filter_anchors(review.comments, sized.files)
        summary = review.summary
        if folded:
            summary = _append_folded_bullets(summary, folded)

        _enforce_redaction(summary, kept)

        # Italicized notice prepended above the Verdict line so it reads as
        # bot infrastructure metadata rather than review content. The
        # sign-off invariant ("last non-empty line is `— daeyeon-bot 🐥`")
        # is preserved because we only touch the head of the body.
        is_force_supersede = sized.force and already_posted and prior is not None
        if is_force_supersede and prior is not None and prior.submitted_at is not None:
            header = (
                f"_Updated review for SHA `{sized.head_sha}`"
                f" — supersedes earlier bot review posted at"
                f" {prior.submitted_at.strftime('%H:%M:%S UTC')}._"
            )
            summary = header + "\n\n" + summary

        # APPROVE → GH APPROVE event (counts toward branch protection);
        # everything else → COMMENT. The schema validator already enforces
        # `verdict=APPROVE ⇔ comments==[]`, so we trust the verdict field.
        # Self-authored PRs (review_self) can never APPROVE — GitHub rejects a
        # self-approval (HTTP 422) — so an APPROVE verdict is downgraded to a
        # COMMENT review carrying the same (empty-comments) summary body.
        is_self = sized.author_login == self.github_username
        gh_event = "APPROVE" if (review.verdict == "APPROVE" and not is_self) else "COMMENT"
        # House style: a real APPROVE earns a celebratory LGTM GIF in the
        # Summary (operator preference). Only on the posted APPROVE event —
        # COMMENT/REQUEST_CHANGES (incl. self-PRs downgraded to COMMENT) stay
        # text-only. Inserted above the sign-off and after redaction; the GIF
        # URL is a vetted constant so it can't trip the redaction guard.
        if gh_event == "APPROVE":
            summary = _insert_above_signoff(summary, pick_lgtm_gif(sized.head_sha))
        posted = await self.gh.post_review(
            sized.repo,
            sized.pr_number,
            commit_id=sized.head_sha,
            body=summary,
            comments=[inline_to_api(c) for c in kept],
            event=gh_event,
            login=self.github_username,
        )
        new_review_id = _read_review_id(posted)
        new_submitted_at = _read_submitted_at(posted)

        if is_force_supersede and prior is not None and new_submitted_at is not None:
            await record_supersede(
                self.db,
                prior.id,
                new_review_id=new_review_id or 0,
                new_submitted_at=new_submitted_at,
            )
            await self.db.commit()
        else:
            await self._write_audit(
                **sized.audit_kwargs,
                status="posted",
                review_id=new_review_id,
                submitted_at=new_submitted_at,
                summary_chars=len(summary),
                inline_comment_count=len(kept),
                created_at=now,
            )

        _log.info(
            "pr_review.posted",
            repo=sized.repo,
            pr_number=sized.pr_number,
            head_sha=sized.head_sha,
            review_id=new_review_id,
            inline_count=len(kept),
            superseded=is_force_supersede,
        )
        return Ack()

    async def _write_audit(self, **kwargs: Any) -> None:
        await insert_audit(self.db, **kwargs)
        await self.db.commit()

    async def _call_claude_with_retry(
        self,
        *,
        ctx: HandlerContext,
        system_prompt: str,
        user_message: str,
    ) -> ReviewOutput:
        last_error: str | None = None
        for attempt in (0, 1):
            session = ctx.claude_session_factory()
            async with session as s:  # type: ignore[attr-defined]
                response_obj = await s.query(  # type: ignore[attr-defined]
                    user_message
                    if last_error is None
                    else (
                        user_message
                        + "\n\n---\nYour previous response failed validation: "
                        + last_error
                        + "\nReturn ONLY the corrected JSON object."
                    ),
                    system=system_prompt,
                )
            response = cast("str", response_obj)
            try:
                parsed = json.loads(_strip_code_fence(response))
            except (TypeError, ValueError) as exc:
                last_error = f"JSON parse: {exc}"
                if attempt == 1:
                    raise PermanentError(
                        f"claude returned malformed review (parse): {exc}"
                    ) from exc
                continue
            try:
                return ReviewOutput.model_validate(parsed)
            except PydanticValidationError as exc:
                last_error = exc.errors().__repr__()
                if attempt == 1:
                    raise PermanentError("claude returned malformed review (schema)") from exc
                continue
        raise PermanentError("unreachable: claude retry loop fell through")


# ── pipeline state ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Parsed:
    """Inputs parsed straight from the Event payload (pre-pr_get)."""

    repo: str
    pr_number: int
    head_sha: str  # payload snapshot — may be stale by `gh.pr_get` time
    request_gen: int
    force: bool
    # True when the event arrived via the operator's explicit `dev
    # fire-pr-review` CLI (`pr.review.manual`), as opposed to the
    # `gh.review_requested` auto-poller. An explicit manual fire is the
    # operator's own authorization, so it bypasses the scope gates
    # (allowlist / self-authored / withdrawn).
    is_manual: bool


@dataclass(frozen=True, slots=True)
class _PrepState:
    """Stages (a)+(b) output: persona + fresh PR metadata."""

    event_id: str
    repo: str
    pr_number: int
    request_gen: int
    force: bool
    is_manual: bool
    head_sha: str  # current head from pr_get (or payload if pr_get omitted it)
    persona: Persona
    pr: dict[str, Any]
    author_login: str
    requested_logins: tuple[str, ...]
    pr_state: str
    audit_kwargs: dict[str, Any]
    now: datetime


@dataclass(frozen=True, slots=True)
class _SizedState:
    """`_PrepState` + the file list and counts from `gh.pr_files`."""

    event_id: str
    repo: str
    pr_number: int
    request_gen: int
    force: bool
    head_sha: str
    persona: Persona
    pr: dict[str, Any]
    author_login: str
    audit_kwargs: dict[str, Any]
    files: list[dict[str, Any]]
    n_files: int
    n_lines: int

    @classmethod
    def from_prep(
        cls,
        prep: _PrepState,
        *,
        files: list[dict[str, Any]],
        n_files: int,
        n_lines: int,
    ) -> _SizedState:
        return cls(
            event_id=prep.event_id,
            repo=prep.repo,
            pr_number=prep.pr_number,
            request_gen=prep.request_gen,
            force=prep.force,
            head_sha=prep.head_sha,
            persona=prep.persona,
            pr=prep.pr,
            author_login=prep.author_login,
            audit_kwargs=prep.audit_kwargs,
            files=files,
            n_files=n_files,
            n_lines=n_lines,
        )


# ── module-level helpers ──────────────────────────────────────────────────


def _parse_payload(event: Event) -> _Parsed:
    payload = event.payload
    repo = str(payload.get("repo", ""))
    pr_number_raw = payload.get("pr_number")
    head_sha = str(payload.get("head_sha", ""))
    # `request_gen` is INT in the schema and the trigger emits int; tolerate
    # legacy string payloads (queued before A5) so an in-flight outbox row
    # from a pre-fix daemon won't fail validation on the next boot.
    request_gen_raw = payload.get("request_gen", 0)
    try:
        request_gen = int(request_gen_raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"pr_review payload request_gen not an int: {request_gen_raw!r}"
        ) from exc
    force = bool(payload.get("force", False))
    if not repo or pr_number_raw is None or not head_sha:
        raise ValidationError("pr_review payload must include repo, pr_number, head_sha")
    return _Parsed(
        repo=repo,
        pr_number=int(pr_number_raw),
        head_sha=head_sha,
        request_gen=request_gen,
        force=force,
        is_manual=event.type == "pr.review.manual",
    )


def _read_head_sha(pr: dict[str, Any]) -> str | None:
    head = pr.get("head")
    if isinstance(head, dict):
        sha = head.get("sha")  # type: ignore[union-attr]
        if isinstance(sha, str):
            return sha
    return None


def _read_author(pr: dict[str, Any]) -> str:
    user = pr.get("user")
    if isinstance(user, dict):
        login = user.get("login")  # type: ignore[union-attr]
        if isinstance(login, str):
            return login
    return ""


def _read_requested_logins(pr: dict[str, Any]) -> tuple[str, ...]:
    raw = pr.get("requested_reviewers", [])
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for entry in raw:  # type: ignore[assignment]
        if isinstance(entry, dict):
            login = entry.get("login")  # type: ignore[union-attr]
            if isinstance(login, str):
                out.append(login)
    return tuple(out)


def _normalize_file(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": str(raw.get("filename") or raw.get("path") or ""),
        "additions": int(raw.get("additions") or 0),
        "deletions": int(raw.get("deletions") or 0),
        "status": str(raw.get("status") or "modified"),
        "patch": raw.get("patch") if isinstance(raw.get("patch"), str) else None,
    }


def _read_review_id(posted: dict[str, Any]) -> int | None:
    raw = posted.get("id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _read_submitted_at(posted: dict[str, Any]) -> datetime | None:
    raw = posted.get("submitted_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # GitHub returns "Z"-suffixed ISO; Python 3.12 fromisoformat handles it.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_code_fence(text: str) -> str:
    """Tolerate Claude wrapping its JSON in ```json … ``` despite the prompt."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop first line (``` or ```json) and trailing ``` if present.
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _is_repo_allowed(repo: str, allowed: list[str]) -> bool:
    """Return True if `repo` matches any glob in `allowed`, or `allowed` is empty.

    Empty `allowed` means no filter (legacy behavior). Globs use `fnmatch`
    semantics, so `rebellions-sw/*` matches the whole org and explicit
    `rebellions-sw/daeyeon-bot` matches just that repo. Case-insensitive
    because GitHub treats `Owner/Repo` and `owner/repo` as the same path.
    """
    if not allowed:
        return True
    repo_lc = repo.lower()
    return any(fnmatch.fnmatchcase(repo_lc, pat.lower()) for pat in allowed)


def _filter_anchors(
    comments: list[InlineComment],
    files: list[dict[str, Any]],
) -> tuple[list[InlineComment], list[InlineComment]]:
    """Split `comments` into (kept, folded). Folded ones go into Summary bullets."""
    by_path: dict[str, list[tuple[int, int]]] = {}
    for f in files:
        patch = f.get("patch")
        path = str(f.get("filename", ""))
        if not path or not isinstance(patch, str):
            continue
        by_path[path] = parse_hunk_ranges(patch)
    kept: list[InlineComment] = []
    folded: list[InlineComment] = []
    for comment in comments:
        hunks = by_path.get(comment.path)
        if hunks is None:
            folded.append(comment)
            continue
        if is_anchor_in_hunk(comment.line, comment.start_line, hunks):
            kept.append(comment)
        else:
            folded.append(comment)
    return (kept, folded)


_SIGNOFF_PREFIX = "— daeyeon-bot 🐥"


def _insert_above_signoff(summary: str, block: str) -> str:
    """Inject `block` just above the sign-off line, keeping it the last line.

    `pr_review_prompt.OUTPUT_DIRECTIVE` requires the very last non-empty line to be the
    sign-off marker. Naively appending after the summary breaks that
    invariant whenever Claude obeyed the directive (which is the common
    path). We split on the last line whose first non-whitespace tokens
    are the sign-off marker and inject `block` above it. Falls back
    to plain append when no sign-off is present (e.g. older summaries
    without sign-off, or chat-mode callers).
    """
    if not block:
        return summary
    lines = summary.split("\n")
    signoff_idx: int | None = None
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].lstrip().startswith(_SIGNOFF_PREFIX):
            signoff_idx = idx
            break
    if signoff_idx is None:
        if summary.endswith("\n"):
            return summary + "\n" + block
        return summary + "\n\n" + block
    head = lines[:signoff_idx]
    while head and head[-1] == "":
        head.pop()
    tail = lines[signoff_idx:]
    rebuilt = [*head, "", block, "", *tail]
    return "\n".join(rebuilt)


def _append_folded_bullets(summary: str, folded: list[InlineComment]) -> str:
    """Fold out-of-hunk inline comments into Summary bullets above the sign-off."""
    bullets = "\n".join(f"- [{c.path} near L{c.line}] {c.body}" for c in folded)
    return _insert_above_signoff(summary, bullets)


def _enforce_redaction(summary: str, comments: list[InlineComment]) -> None:
    """Two-tier redaction guard (FR-015 + A4).

    Named-token hits (Slack / AWS / JWT / Anthropic / GitHub) raise
    `PermanentError` so the row goes to DLQ — that's a real secret leaking
    from the model and we must not post it. Entropy-only hits are logged as
    `pr_review.redaction_entropy` and the original text is posted unchanged
    — the entropy heuristic's false-positive rate on natural review prose
    (long hashes, identifiers, code excerpts) is too high to gate posts on.
    Log-sink redaction (`infra/logging.py:redact_processor`) keeps its
    strict behavior — this loosening applies only to posted PR content.
    """
    _check_named_redaction("summary", summary)
    for comment in comments:
        _check_named_redaction(f"comment on {comment.path}", comment.body)


def _check_named_redaction(label: str, text: str) -> None:
    _, spans = redact_with_provenance(text)
    named = [(start, end, reason) for start, end, reason in spans if reason != "entropy"]
    if named:
        reasons = sorted({reason for _, _, reason in named})
        raise PermanentError(f"redaction would alter posted content ({label}); reasons={reasons}")
    entropy_spans: list[tuple[int, int, RedactReason]] = [
        (start, end, reason) for start, end, reason in spans if reason == "entropy"
    ]
    if entropy_spans:
        _log.warning(
            "pr_review.redaction_entropy",
            label=label,
            spans=[(start, end) for start, end, _ in entropy_spans],
        )


__all__ = ["MANIFEST", "PrReviewHandler"]
