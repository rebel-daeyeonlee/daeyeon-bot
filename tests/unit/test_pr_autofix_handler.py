"""Feature 004 — `pr_autofix` handler gates and the fix/push pipeline.

The handler is the daemon's only writer to a git remote, so most of these tests
are about the paths where it must NOT write: someone else's PR, a closed PR, an
exhausted round budget, a protected path, an oversized diff, a failed verify.

The fix stage runs against a real local git clone (a bare repo in `tmp_path`
standing in for GitHub) with a fake agent session that edits files the way a
real agent would. Faking `git diff` instead would test the fake.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import aiosqlite
import pytest

from daeyeon_bot.app.config import PrAutofixHandlerEntry
from daeyeon_bot.core.events import make_event
from daeyeon_bot.core.results import Ack, DeadLetter
from daeyeon_bot.handlers.pr_autofix import MANIFEST, PrAutofixHandler
from daeyeon_bot.infra import outbox, pr_autofix_audit, pr_autofix_ledger
from daeyeon_bot.infra.git_workspace import GitWorkspace
from daeyeon_bot.infra.persona_loader import PersonaLoader
from daeyeon_bot.infra.storage import apply_migrations, open_db
from tests.fakes.gh_cli import FakeGh

REPO = "rebel-daeyeonlee/daeyeon-bot"
PR = 9
OPERATOR = "daeyeon-lee"
SHA = "a" * 40
NOW = datetime(2026, 1, 1, tzinfo=UTC)

PERSONA = "# autofix persona\n\n" + ("리뷰 코멘트를 판정하고 고친다. " * 20)


# ── doubles ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _FixedClock:
    """Structurally a `Clock` — the protocol also declares `monotonic()`, which
    this handler never reads, so it is stubbed rather than left missing."""

    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0.0


@dataclass(slots=True)
class _Ctx:
    clock: Any
    trace_id: str
    claude_session_factory: Any


@dataclass(slots=True)
class _TriageSession:
    """Text-only session standing in for the triage stage."""

    replies: list[str]

    async def __aenter__(self) -> _TriageSession:
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None

    async def query(self, prompt: str, *, system: str | None = None) -> str:
        del prompt, system
        return self.replies.pop(0) if self.replies else "{}"


@dataclass(slots=True)
class _AgentSession:
    """Tool-enabled session stand-in. `edit` runs the side effect a real agent
    would have had, so the handler's `git diff` sees genuine changes."""

    cwd: Path
    edit: Callable[[Path], None] | None = None
    reply: str = "- [comment #11] 경계 조건을 고쳤습니다 (app.py:1)"
    calls: list[str] = field(default_factory=list)

    async def __aenter__(self) -> _AgentSession:
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None

    async def query(self, prompt: str, *, system: str | None = None) -> str:
        del system
        self.calls.append(prompt)
        if self.edit is not None:
            self.edit(self.cwd)
        return self.reply


def _write(relative: str, content: str) -> Callable[[Path], None]:
    """The side effect a real fix agent would have had on the working tree."""

    def _edit(cwd: Path) -> None:
        (cwd / relative).write_text(content)

    return _edit


def _triage_json(*decisions: dict[str, Any]) -> str:
    return json.dumps({"decisions": list(decisions)})


def _accept(cid: int = 11) -> dict[str, Any]:
    return {
        "comment_id": cid,
        "verdict": "accepted",
        "reasoning": "지적이 맞습니다.",
        "fix_instruction": "app.py의 경계 조건을 고칩니다.",
        "evidence": "app.py:1",
    }


def _reject(cid: int = 11) -> dict[str, Any]:
    return {
        "comment_id": cid,
        "verdict": "rejected",
        "reasoning": "`app.py:1`에서 이미 검사하고 있어 도달하지 않습니다.",
        "fix_instruction": "",
        "evidence": "app.py:1",
    }


# ── fixtures ──────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e.com",
        },
    )


@pytest.fixture
def persona_root(tmp_path: Path) -> Path:
    skill = tmp_path / "skills" / "daeyeon-bot-pr-autofix"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(PERSONA, encoding="utf-8")
    return tmp_path / "skills"


@pytest.fixture
def workspace_factory(tmp_path: Path) -> Any:
    """A `GitWorkspace` already cloned from a local bare repo, so the pipeline
    can commit for real without a GitHub remote."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "app.py").write_text("x = 1\n")
    (seed / ".github").mkdir()
    (seed / ".github" / "ci.yml").write_text("on: push\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    _git(seed, "clone", "-q", "--bare", str(seed), str(bare))
    # GitHub mirrors every PR head into the BASE repo's `refs/pull/<n>/head`;
    # `checkout_pr` fetches through that ref because a fork's branch does not
    # exist on origin. Reproduce it so the local stand-in behaves the same.
    _git(bare, "update-ref", f"refs/pull/{PR}/head", "refs/heads/main")

    made: list[GitWorkspace] = []

    def _factory(repo: str) -> GitWorkspace:
        ws = GitWorkspace(
            repo=repo,
            workspace_root=tmp_path / "ws",
            allow_external=True,
            remote_url_override=str(bare),
        )
        ws.workspace_root.mkdir(parents=True, exist_ok=True)
        made.append(ws)
        return ws

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=seed,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    _factory.made = made  # type: ignore[attr-defined]
    _factory.head_sha = head  # type: ignore[attr-defined]
    return _factory


async def _db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


def _build(
    *,
    db: aiosqlite.Connection,
    gh: FakeGh,
    persona_root: Path,
    triage: list[str] | None = None,
    agent_edit: Callable[[Path], None] | None = None,
    agent_reply: str | None = None,
    workspace_factory: Any = None,
    **config_kw: Any,
) -> tuple[PrAutofixHandler, _Ctx, list[_AgentSession]]:
    entry = PrAutofixHandlerEntry.model_validate(
        {
            "persona_skill": "daeyeon-bot-pr-autofix",
            "allowed_repos": [],
            # Default off: most tests do not want a push. The bare repo in
            # `workspace_factory` accepts one, so tests that need the real path
            # pass `push_enabled=True`.
            "push_enabled": False,
            **config_kw,
        }
    )
    sessions: list[_AgentSession] = []

    def _agent_factory(*, cwd: Path) -> _AgentSession:
        kwargs: dict[str, Any] = {"cwd": cwd, "edit": agent_edit}
        if agent_reply is not None:
            kwargs["reply"] = agent_reply
        session = _AgentSession(**kwargs)
        sessions.append(session)
        return session

    handler = PrAutofixHandler(
        manifest=MANIFEST,
        gh=gh,  # type: ignore[arg-type]
        persona_loader=PersonaLoader(skills_root=persona_root),
        config=entry,
        github_username=OPERATOR,
        db=db,
        agent_session_factory=_agent_factory,
        workspace_factory=workspace_factory,
    )
    ctx = _Ctx(
        clock=_FixedClock(),
        trace_id="t",
        claude_session_factory=lambda: _TriageSession(list(triage or [])),
    )
    return handler, ctx, sessions


def _make_event(event_type: str = "gh.pr_feedback", **payload_kw: Any) -> Any:
    payload: dict[str, Any] = {
        "repo": REPO,
        "pr_number": PR,
        "head_sha": SHA,
        "round": 1,
        "comment_ids": [11],
    }
    payload.update(payload_kw)
    return make_event(type=event_type, payload=payload, created_at=NOW)


_dedup_seq = 0


async def _event(conn: aiosqlite.Connection, event_type: str = "gh.pr_feedback", **kw: Any) -> Any:
    """Build an event AND persist it. `pr_autofix_audit.event_id` has a real FK
    to `events(id)` — an audit row for a phantom event would be untraceable."""
    global _dedup_seq
    _dedup_seq += 1
    event = _make_event(event_type, **kw)
    await outbox.insert_event(conn, event, source="test", source_dedup_key=f"test-{_dedup_seq}")
    await conn.commit()
    return event


def _seed_pr(gh: FakeGh, *, author: str = OPERATOR, head_sha: str = SHA, **kw: Any) -> None:
    gh.add_pr(
        REPO,
        PR,
        head_sha=head_sha,
        author=author,
        in_authored_set=True,
        files=[
            {
                "filename": "app.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "patch": "@@ -1 +1,2 @@\n x = 1\n+y = 2",
            }
        ],
        **kw,
    )


async def _audit(conn: aiosqlite.Connection, event_id: str) -> Any:
    return await pr_autofix_audit.find_by_event(conn, event_id)


# ── the manifest contract ─────────────────────────────────────────────────


def test_handler_is_not_idempotent() -> None:
    """`git push` has no undo, so an `interrupted` row must reach dead_letter
    for the operator rather than being silently re-claimed and pushed twice."""
    assert MANIFEST.idempotent is False


def test_handler_serializes_on_a_side_effect_key() -> None:
    assert MANIFEST.side_effect_key == "pr_autofix"


# ── gates that must not write ─────────────────────────────────────────────


async def test_skips_a_pr_authored_by_someone_else(tmp_path: Path, persona_root: Path) -> None:
    """THE boundary. The bot never pushes to a branch that is not the operator's."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, author="somebody-else")
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="fix")
        handler, ctx, _ = _build(db=conn, gh=gh, persona_root=persona_root)
        event = await _event(conn)

        assert isinstance(await handler.handle(event, ctx), Ack)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "skipped_not_author"
        assert gh.replies == [] and gh.issue_comments_posted == []
    finally:
        await conn.close()


async def test_author_gate_reads_the_live_pr_not_the_event(
    tmp_path: Path, persona_root: Path
) -> None:
    """A PR can change hands between the trigger's emit and this dispatch; the
    payload is not evidence of who owns the branch now."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, author="NEW-owner")
        handler, ctx, _ = _build(db=conn, gh=gh, persona_root=persona_root)
        row_result = await handler.handle(await _event(conn), ctx)  # type: ignore[arg-type]
        assert isinstance(row_result, Ack)
    finally:
        await conn.close()


async def test_skips_a_closed_pr(tmp_path: Path, persona_root: Path) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, state="closed")
        handler, ctx, _ = _build(db=conn, gh=gh, persona_root=persona_root)
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "skipped_closed"
    finally:
        await conn.close()


async def test_skips_a_merged_pr(tmp_path: Path, persona_root: Path) -> None:
    """GitHub reports a merged PR with `state: closed`, but a stale cache or a
    payload shape change could leave `state` open — check `merged_at` too."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, merged_at="2026-01-01T00:00:00Z")
        handler, ctx, _ = _build(db=conn, gh=gh, persona_root=persona_root)
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "skipped_closed"
    finally:
        await conn.close()


async def test_skips_a_repo_outside_the_allowlist(tmp_path: Path, persona_root: Path) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        handler, ctx, _ = _build(
            db=conn, gh=gh, persona_root=persona_root, allowed_repos=["other/*"]
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "skipped_disallowed_repo"
    finally:
        await conn.close()


async def test_manual_fire_bypasses_the_repo_allowlist_but_not_the_author_gate(
    tmp_path: Path, persona_root: Path
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, author="somebody-else")
        handler, ctx, _ = _build(
            db=conn, gh=gh, persona_root=persona_root, allowed_repos=["nothing/matches"]
        )
        event = await _event(conn, "pr.autofix.manual")
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "skipped_not_author"
    finally:
        await conn.close()


async def test_nothing_pending_when_every_comment_is_already_in_the_ledger(
    tmp_path: Path, persona_root: Path
) -> None:
    """The termination case. Once answered, a comment never comes back."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="fix")
        await pr_autofix_ledger.record_decision(
            conn,
            repo=REPO,
            pr_number=PR,
            comment_id=11,
            comment_kind="review_comment",
            author="coderabbitai[bot]",
            verdict="rejected",
            created_at=NOW,
        )
        handler, ctx, _ = _build(db=conn, gh=gh, persona_root=persona_root)
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "skipped_nothing_pending"
    finally:
        await conn.close()


async def test_max_rounds_stands_down_and_says_so_once(tmp_path: Path, persona_root: Path) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="fix")
        handler, ctx, _ = _build(db=conn, gh=gh, persona_root=persona_root, max_rounds=2)

        first = await _event(conn, round=3)
        await handler.handle(first, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, first.id)
        assert row is not None and row.status == "skipped_max_rounds"
        assert len(gh.issue_comments_posted) == 1
        assert "라운드를 모두 소진" in gh.issue_comments_posted[0]["body"]

        # A second trip must not spam the PR again.
        await handler.handle(await _event(conn, round=4), ctx)  # type: ignore[arg-type]
        assert len(gh.issue_comments_posted) == 1
    finally:
        await conn.close()


async def test_max_rounds_zero_disables_the_brake(tmp_path: Path, persona_root: Path) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="fix")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            max_rounds=0,
            triage=[_triage_json(_reject())],
        )
        event = await _event(conn, round=99)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "all_rejected"
    finally:
        await conn.close()


# ── triage-only outcomes ──────────────────────────────────────────────────


async def test_all_rejected_replies_and_never_touches_git(
    tmp_path: Path, persona_root: Path
) -> None:
    """The operator asked for a reply on refusal too — and a refusal must cost
    nothing on the git side."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(
            REPO, PR, comment_id=11, author="coderabbitai[bot]", body="use a set here"
        )
        handler, ctx, sessions = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_reject())],
            workspace_factory=None,
        )
        event = await _event(conn)

        assert isinstance(await handler.handle(event, ctx), Ack)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "all_rejected"
        assert row.rejected_count == 1
        assert sessions == []  # no fix agent was ever opened
        assert len(gh.replies) == 1
        assert "수정하지 않았습니다" in gh.replies[0]["body"]
        assert "이미 검사하고 있어" in gh.replies[0]["body"]
        assert await pr_autofix_ledger.handled_comment_ids(conn, repo=REPO, pr_number=PR) == {11}
    finally:
        await conn.close()


async def test_reply_goes_to_the_thread_root(tmp_path: Path, persona_root: Path) -> None:
    """GitHub 422s on a reply-to-a-reply."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(REPO, PR, comment_id=10, author="coderabbitai[bot]", body="root")
        gh.add_review_comment(
            REPO,
            PR,
            comment_id=11,
            author="coderabbitai[bot]",
            body="still broken",
            in_reply_to_id=10,
        )
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_reject(10), _reject(11))],
        )
        await handler.handle(await _event(conn), ctx)  # type: ignore[arg-type]
        assert {r["in_reply_to_id"] for r in gh.replies} == {10}
    finally:
        await conn.close()


async def test_non_inline_feedback_is_folded_into_one_comment(
    tmp_path: Path, persona_root: Path
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review(REPO, PR, review_id=21, author="copilot[bot]", body="summary finding")
        gh.add_issue_comment(REPO, PR, comment_id=22, author="colleague", body="스타일 통일 부탁")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_reject(21), _reject(22))],
        )
        await handler.handle(await _event(conn, comment_ids=[21, 22]), ctx)  # type: ignore[arg-type]
        assert gh.replies == []
        assert len(gh.issue_comments_posted) == 1
        body = gh.issue_comments_posted[0]["body"]
        assert "summary finding" in body
        assert "스타일 통일 부탁" in body
    finally:
        await conn.close()


async def test_a_comment_the_model_forgot_is_deferred_not_dropped(
    tmp_path: Path, persona_root: Path
) -> None:
    """Dropping it would leave the comment pending forever and the loop would
    never converge."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        gh.add_review_comment(REPO, PR, comment_id=12, author="coderabbitai[bot]", body="b")
        handler, ctx, _ = _build(
            db=conn, gh=gh, persona_root=persona_root, triage=[_triage_json(_reject(11))]
        )
        await handler.handle(await _event(conn), ctx)  # type: ignore[arg-type]
        rows = {
            r.comment_id: r.verdict
            for r in await pr_autofix_ledger.list_for_pr(conn, repo=REPO, pr_number=PR)
        }
        assert rows == {11: "rejected", 12: "deferred"}
        assert len(gh.replies) == 2
    finally:
        await conn.close()


async def test_invalid_triage_json_retries_once_then_dead_letters(
    tmp_path: Path, persona_root: Path
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="x")
        handler, ctx, _ = _build(
            db=conn, gh=gh, persona_root=persona_root, triage=["not json", "still not json"]
        )
        from daeyeon_bot.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await handler.handle(await _event(conn), ctx)  # type: ignore[arg-type]
    finally:
        await conn.close()


async def test_triage_recovers_from_prose_around_the_json(
    tmp_path: Path, persona_root: Path
) -> None:
    """The dominant historical dead-letter cause across this codebase's handlers
    was a leading sentence before the JSON object."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="x")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[f"판정 결과입니다:\n```json\n{_triage_json(_reject())}\n```\n"],
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "all_rejected"
    finally:
        await conn.close()


# ── the fix pipeline ──────────────────────────────────────────────────────


async def test_accepted_fix_commits_and_replies_with_the_sha(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(
            REPO, PR, comment_id=11, author="coderabbitai[bot]", body="off by one"
        )
        handler, ctx, sessions = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=True,
        )
        event = await _event(conn)

        assert isinstance(await handler.handle(event, ctx), Ack)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None
        assert row.status == "pushed"
        assert row.accepted_count == 1
        assert row.commit_sha is not None and len(row.commit_sha) == 40
        assert row.changed_files == 1
        assert len(gh.replies) == 1
        assert "수정했습니다" in gh.replies[0]["body"]
        assert row.commit_sha[:8] in gh.replies[0]["body"]
        # The agent got only the accepted item's brief.
        assert "app.py의 경계 조건" in sessions[0].calls[0]
    finally:
        await conn.close()


async def test_commit_message_names_every_comment_it_answers(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """A human reading `git log` months later needs a route back to the thread."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=True,
        )
        await handler.handle(await _event(conn), ctx)  # type: ignore[arg-type]
        ws = workspace_factory.made[0]
        message = await asyncio.to_thread(
            lambda: (
                subprocess.run(
                    ["git", "log", "-1", "--pretty=%B"],
                    cwd=ws.path,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
        )
        assert "#11" in message
        assert "coderabbitai[bot]" in message
        assert "fix(review)" in message
    finally:
        await conn.close()


async def test_agent_that_changes_nothing_is_reported_honestly(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """Claiming a fix that is not on the branch is worse than admitting the miss:
    reviewers would stop checking."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=None,
            workspace_factory=workspace_factory,
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "no_changes"
        assert "수정했습니다" not in gh.replies[0]["body"]
        assert "처리하지 못했습니다" in gh.replies[0]["body"]
        # Ledger records `failed`, not `accepted` — no false trail.
        rows = await pr_autofix_ledger.list_for_pr(conn, repo=REPO, pr_number=PR)
        assert rows[0].verdict == "failed"
    finally:
        await conn.close()


async def test_protected_path_edit_is_reverted_and_not_committed(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write(".github/ci.yml", "on: pwn\n"),
            workspace_factory=workspace_factory,
            protected_paths=[".github/**"],
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "skipped_protected_path"
        assert row.error is not None and ".github/ci.yml" in row.error
        ws = workspace_factory.made[0]
        assert (ws.path / ".github" / "ci.yml").read_text() == "on: push\n"
    finally:
        await conn.close()


async def test_oversized_diff_is_reverted_and_not_committed(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "y\n" * 50),
            workspace_factory=workspace_factory,
            max_changed_lines=5,
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "skipped_diff_too_large"
        assert (await workspace_factory.made[0].diff()).is_empty
    finally:
        await conn.close()


async def test_failed_verify_blocks_the_push_and_leaves_comments_pending(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """A failed verify is a 'not yet', not a verdict on the feedback — so the
    ledger stays empty and the next poll retries."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            verify_commands={REPO: "echo broken && exit 1"},
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None
        assert row.status == "verify_failed"
        assert row.verify_exit_code == 1
        assert row.commit_sha is None
        assert "push하지 않았습니다" in gh.issue_comments_posted[0]["body"]
        assert await pr_autofix_ledger.handled_comment_ids(conn, repo=REPO, pr_number=PR) == set()
        assert (await workspace_factory.made[0].diff()).is_empty
    finally:
        await conn.close()


async def test_passing_verify_lets_the_commit_through(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=True,
            verify_commands={REPO: "true"},
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "pushed"
        assert row.verify_exit_code == 0
        assert "`true` → 통과" in gh.replies[0]["body"]
    finally:
        await conn.close()


async def test_a_repo_with_no_verify_command_is_pushed_unverified(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _ = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=True,
            verify_commands={"some/other-repo": "exit 1"},
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None and row.status == "pushed"
        assert row.verify_command is None
    finally:
        await conn.close()


# ── re-entry after a crash ────────────────────────────────────────────────


async def test_reentry_on_a_pushed_event_is_a_no_op(tmp_path: Path, persona_root: Path) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        handler, ctx, _ = _build(db=conn, gh=gh, persona_root=persona_root)
        event = await _event(conn)
        await pr_autofix_audit.insert_audit(
            conn,
            event_id=event.id,
            repo=REPO,
            pr_number=PR,
            head_sha=SHA,
            round=1,
            status="pushed",
            created_at=NOW,
            commit_sha="b" * 40,
        )
        await conn.commit()

        assert isinstance(await handler.handle(event, ctx), Ack)  # type: ignore[arg-type]
        assert gh.replies == [] and gh.issue_comments_posted == []
    finally:
        await conn.close()


async def test_reentry_on_an_in_progress_event_dead_letters(
    tmp_path: Path, persona_root: Path
) -> None:
    """We died between 'about to commit' and 'pushed'. Retrying could put a
    duplicate commit on the branch — that call belongs to the operator."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        handler, ctx, _ = _build(db=conn, gh=gh, persona_root=persona_root)
        event = await _event(conn)
        await pr_autofix_audit.insert_audit(
            conn,
            event_id=event.id,
            repo=REPO,
            pr_number=PR,
            head_sha=SHA,
            round=1,
            status="in_progress",
            created_at=NOW,
        )
        await conn.commit()

        result = await handler.handle(event, ctx)  # type: ignore[arg-type]
        assert isinstance(result, DeadLetter)
        assert "interrupted mid-push" in result.reason
    finally:
        await conn.close()


async def test_a_prior_skip_does_not_block_a_later_attempt(
    tmp_path: Path, persona_root: Path
) -> None:
    """Only `pushed` and `in_progress` are terminal for re-entry."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _ = _build(
            db=conn, gh=gh, persona_root=persona_root, triage=[_triage_json(_reject())]
        )
        event = await _event(conn)
        await pr_autofix_audit.insert_audit(
            conn,
            event_id=event.id,
            repo=REPO,
            pr_number=PR,
            head_sha=SHA,
            round=1,
            status="skipped_nothing_pending",
            created_at=NOW,
        )
        await conn.commit()
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        assert len(gh.replies) == 1
    finally:
        await conn.close()


# ── agent-discovered verify command ───────────────────────────────────────


def test_parse_verify_command_reads_the_agents_line() -> None:
    from daeyeon_bot.handlers.pr_autofix_prompt import parse_verify_command

    assert (
        parse_verify_command("- [comment #1] 고침\nVERIFY: uv run pytest inv -q")
        == "uv run pytest inv -q"
    )


def test_parse_verify_command_treats_none_as_no_command() -> None:
    """The agent must be able to say 'I could not work one out' — forcing it to
    invent a command would produce a confident-looking failure on a repo whose
    CI cannot run here at all."""
    from daeyeon_bot.handlers.pr_autofix_prompt import parse_verify_command

    for reply in ("VERIFY: none", "VERIFY: 없음", "VERIFY: -", "VERIFY:   N/A"):
        assert parse_verify_command(reply) is None, reply


def test_parse_verify_command_ignores_a_mention_mid_line() -> None:
    """Anchored to line start, so prose about the directive is not the directive."""
    from daeyeon_bot.handlers.pr_autofix_prompt import parse_verify_command

    assert parse_verify_command("나는 VERIFY: rm -rf / 라고 쓰지 않았다") is None


def test_parse_verify_command_takes_the_last_line() -> None:
    from daeyeon_bot.handlers.pr_autofix_prompt import parse_verify_command

    assert parse_verify_command("VERIFY: first\ntext\nVERIFY: second") == "second"


def test_parse_verify_command_rejects_an_absurdly_long_command() -> None:
    from daeyeon_bot.handlers.pr_autofix_prompt import parse_verify_command

    assert parse_verify_command("VERIFY: " + "x" * 2000) is None


def test_parse_verify_command_handles_no_line_at_all() -> None:
    from daeyeon_bot.handlers.pr_autofix_prompt import parse_verify_command

    assert parse_verify_command("- [comment #1] 고쳤습니다") is None
    assert parse_verify_command("") is None


async def test_agent_reported_command_gates_the_push(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """No config entry, yet a failing agent-chosen command still blocks the push."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _sessions = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            verify_commands={},  # nothing pinned in config
            agent_reply="- [comment #11] 고침\nVERIFY: echo nope && exit 1",
        )

        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None
        assert row.status == "verify_failed"
        assert row.verify_command == "echo nope && exit 1"
        assert row.commit_sha is None
    finally:
        await conn.close()


async def test_agent_reported_command_that_passes_lets_the_push_through(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _sessions = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=True,
            verify_commands={},
            agent_reply="- [comment #11] 고침\nVERIFY: true",
        )

        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None
        assert row.status == "pushed"
        assert row.verify_command == "true"
        assert row.verify_exit_code == 0
    finally:
        await conn.close()


async def test_no_command_anywhere_pushes_and_leaves_it_to_ci(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """`VERIFY: none` and no config entry: push. The PR's own CI is the real
    gate regardless, and refusing to push here would strand the fix."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _sessions = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=True,
            verify_commands={},
            agent_reply="- [comment #11] 고침\nVERIFY: none",
        )

        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None
        assert row.status == "pushed"
        assert row.verify_command is None
    finally:
        await conn.close()


async def test_config_override_beats_the_agents_choice(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """An operator who pins a command means it — the agent does not get a vote."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _sessions = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=True,
            verify_commands={REPO: "true"},
            agent_reply="- [comment #11] 고침\nVERIFY: exit 1",
        )

        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None
        assert row.status == "pushed"
        assert row.verify_command == "true"  # the override, not `exit 1`
    finally:
        await conn.close()


def test_fix_prompt_tells_the_agent_to_discover_and_report(persona_root: Path) -> None:
    """The prompt is the only place the contract lives; a silent edit that drops
    the VERIFY line would make every push unverified without failing anything."""
    from daeyeon_bot.handlers.pr_autofix_prompt import build_fix_system_prompt

    prompt = build_fix_system_prompt("persona", protected_paths=[".github/**"])
    assert "VERIFY:" in prompt
    assert "VERIFY: none" in prompt
    assert ".github/workflows" in prompt


# ── dry run must not claim a push ─────────────────────────────────────────
#
# `push_enabled = false` commits inside the workspace clone, which the next
# round resets. Three separate places could present that as shipped, and all
# three did once: the audit status, the reply body, and the ledger verdict.


async def test_dry_run_records_dry_run_not_pushed(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="coderabbitai[bot]", body="a")
        handler, ctx, _s = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=False,
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]
        row = await _audit(conn, event.id)
        assert row is not None
        assert row.status == "dry_run"
        # `pushed_at` NULL is what separates shipped from not; the local SHA is
        # still kept as the only handle on what the agent produced.
        assert row.pushed_at is None
        assert row.commit_sha is not None
    finally:
        await conn.close()


async def test_dry_run_reply_does_not_claim_a_fix_or_link_a_commit(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """The bug as it actually shipped: reviewers on two real PRs were told
    '수정했습니다' and given a commit URL that 404s."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="Copilot", body="typo")
        handler, ctx, _s = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=False,
        )
        await handler.handle(await _event(conn), ctx)  # type: ignore[arg-type]

        body = gh.replies[0]["body"]
        assert "수정했습니다" not in body
        assert "dry run" in body
        assert "/commit/" not in body, "a dry-run reply must link no commit — it would 404"
        assert "push_enabled" in body  # tells the reviewer why, and how to proceed
    finally:
        await conn.close()


async def test_dry_run_leaves_the_comment_for_a_human_not_marked_done(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """Recording a dry-run accept as `accepted` seals it: turning `push_enabled`
    on later never revisits it, and the fix — which only existed in a wiped
    workspace — is silently lost."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="Copilot", body="typo")
        handler, ctx, _s = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=False,
        )
        await handler.handle(await _event(conn), ctx)  # type: ignore[arg-type]

        rows = await pr_autofix_ledger.list_for_pr(conn, repo=REPO, pr_number=PR)
        assert [r.verdict for r in rows] == ["deferred"]
        assert rows[0].commit_sha is None
        assert "dry run" in (rows[0].reason or "")
    finally:
        await conn.close()


async def test_dry_run_pushes_nothing_to_the_remote(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """The one guarantee a dry run actually makes."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="Copilot", body="typo")
        handler, ctx, _s = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=False,
        )
        await handler.handle(await _event(conn), ctx)  # type: ignore[arg-type]

        ws = workspace_factory.made[0]
        _, remote_branches, _ = await ws._git_run("branch", "-r", what="probe", check=False)
        assert "feature-branch" not in remote_branches
    finally:
        await conn.close()


async def test_a_real_push_still_claims_the_fix_and_links_the_commit(
    tmp_path: Path, persona_root: Path, workspace_factory: Any
) -> None:
    """Guard against over-correcting: with push on, the reply must still be a
    plain success with a working link."""
    conn = await _db(tmp_path)
    try:
        gh = FakeGh(user_login=OPERATOR)
        _seed_pr(gh, head_sha=workspace_factory.head_sha)
        gh.add_review_comment(REPO, PR, comment_id=11, author="Copilot", body="typo")
        handler, ctx, _s = _build(
            db=conn,
            gh=gh,
            persona_root=persona_root,
            triage=[_triage_json(_accept())],
            agent_edit=_write("app.py", "x = 2\n"),
            workspace_factory=workspace_factory,
            push_enabled=True,
        )
        event = await _event(conn)
        await handler.handle(event, ctx)  # type: ignore[arg-type]

        row = await _audit(conn, event.id)
        assert row is not None
        assert row.status == "pushed"
        assert row.pushed_at is not None
        body = gh.replies[0]["body"]
        assert "수정했습니다" in body
        assert "/commit/" in body
        rows = await pr_autofix_ledger.list_for_pr(conn, repo=REPO, pr_number=PR)
        assert [r.verdict for r in rows] == ["accepted"]
    finally:
        await conn.close()
