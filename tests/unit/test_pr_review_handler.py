"""Unit tests for `handlers.pr_review.PrReviewHandler` (T028).

Drives the full state machine from `data-model.md` §4 with `FakeGh`,
`FakeClaudeSession`, `PersonaLoader` over a real SKILL.md, and a real
`aiosqlite` DB in `tmp_path`. Covers each branch of the handler:

  - posts a review (happy path)
  - skipped_self_authored
  - skipped_withdrawn
  - skipped_too_large posts the templated Summary
  - persona_unavailable → ValidationError (DeadLetter via dispatcher)
  - Claude malformed twice → PermanentError (DeadLetter)
  - out-of-hunk anchor folded into Summary
  - force-supersede prepends header + appends old review_id to history
  - redaction: secret in summary → PermanentError, none posted
  - redaction: secret in inline body → PermanentError, none posted
  - clean content posts unchanged
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from daeyeon_bot.app.config import PrReviewHandlerEntry, SizeBudget
from daeyeon_bot.core.errors import PermanentError, ValidationError
from daeyeon_bot.core.events import Event, make_event
from daeyeon_bot.core.results import Ack
from daeyeon_bot.core.time import Clock, SystemClock
from daeyeon_bot.handlers.pr_review import MANIFEST, PrReviewHandler
from daeyeon_bot.infra.claude import FakeClaudeSession, FakeFactory
from daeyeon_bot.infra.pr_review_audit import find_latest
from daeyeon_bot.infra.pr_review_persona import PersonaLoader
from daeyeon_bot.infra.storage import apply_migrations, open_db
from tests.fakes.gh_cli import FakeGh
from tests.fakes.pr_persona import materialize_persona

_PERSONA_BODY = (
    "You are Daeyeon, a thoughtful PR reviewer. Focus on correctness, "
    "tests, and maintainability. Be specific. Avoid praise filler. "
    "Always cite file:line when flagging an issue."
)
_PATCH_HUNK_AT_5 = "@@ -1,3 +5,4 @@\n context\n+added line A\n+added line B\n context\n"
_FILES_ONE_FILE = [
    {
        "filename": "src/foo.py",
        "additions": 2,
        "deletions": 0,
        "status": "modified",
        "patch": _PATCH_HUNK_AT_5,
    }
]


@dataclass(slots=True)
class _Ctx:
    """Tiny HandlerContext stand-in matching the protocol.

    Field types use the protocol types (`Clock`, `Callable[[], object]`)
    rather than concrete implementations so pyright's invariant dataclass
    field check sees this as structurally `HandlerContext`.
    """

    clock: Clock
    trace_id: str
    claude_session_factory: Callable[[], object]


async def _seed_event_row(conn: aiosqlite.Connection, event: Event) -> None:
    await conn.execute(
        "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
        " payload_json, trace_id, created_at)"
        " VALUES (?, ?, ?, 'manual', ?, ?, ?, ?)",
        (
            event.id,
            event.type,
            event.schema_version,
            f"k-{event.id}",
            json.dumps(dict(event.payload)),
            event.trace_id,
            event.created_at.isoformat(),
        ),
    )
    await conn.commit()


async def _build_handler(
    tmp_path: Path,
    *,
    fake_gh: FakeGh,
    persona_body: str = _PERSONA_BODY,
    factory: FakeFactory | None = None,
    config_overrides: PrReviewHandlerEntry | None = None,
) -> tuple[PrReviewHandler, aiosqlite.Connection, FakeClaudeSession]:
    skills_root = tmp_path / "skills"
    materialize_persona(skills_root, "pr-review", body=persona_body)
    loader = PersonaLoader(skills_root=skills_root)
    fake_session = FakeClaudeSession(default="{}")
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    cfg = config_overrides or PrReviewHandlerEntry(
        persona_skill="pr-review",
        min_persona_chars=50,
        size_budget=SizeBudget(max_lines=1000, max_files=50),
    )
    handler = PrReviewHandler(
        manifest=MANIFEST,
        gh=fake_gh,
        persona_loader=loader,
        config=cfg,
        github_username=fake_gh.user_login,
        db=conn,
    )
    return handler, conn, fake_session if factory is None else factory.session


def _manual_event(
    *, repo: str = "o/r", pr_number: int = 7, head_sha: str = "deadbeef", force: bool = False
) -> Event:
    payload = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "request_gen": 0,
        "force": force,
    }
    return make_event(
        type="pr.review.manual",
        payload=payload,
        created_at=datetime.now(tz=UTC),
    )


def _auto_event(
    *, repo: str = "o/r", pr_number: int = 7, head_sha: str = "deadbeef", force: bool = False
) -> Event:
    """A `gh.review_requested` event — the auto-poller path that the scope
    gates (allowlist / self-authored / withdrawn) actually apply to."""
    payload = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "request_gen": 2 if force else 1,
        "force": force,
    }
    return make_event(
        type="gh.review_requested",
        payload=payload,
        created_at=datetime.now(tz=UTC),
    )


def _ctx(factory: FakeFactory | Callable[[], FakeClaudeSession]) -> _Ctx:
    if isinstance(factory, FakeFactory):
        callable_factory: Callable[[], FakeClaudeSession] = factory
    else:
        callable_factory = factory
    return _Ctx(
        clock=SystemClock(),
        trace_id="trace-1",
        claude_session_factory=callable_factory,
    )


@pytest.mark.asyncio
async def test_happy_path_posts_review_and_audit(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "o/r",
        7,
        head_sha="deadbeef",
        author="alice",
        files=_FILES_ONE_FILE,
    )
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {
                    "verdict": "PASS",
                    "summary": "All good for SHA deadbeef.",
                    "comments": [
                        {
                            "path": "src/foo.py",
                            "line": 6,
                            "body": "Nit: line 6 looks suspicious.",
                        }
                    ],
                }
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)

        posted = fake_gh.posted_reviews()
        assert len(posted) == 1
        assert posted[0]["commit_id"] == "deadbeef"
        assert "All good" in posted[0]["body"]
        assert posted[0]["comments"] == [
            {
                "path": "src/foo.py",
                "line": 6,
                "side": "RIGHT",
                "body": "Nit: line 6 looks suspicious.",
            }
        ]

        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "posted"
        assert latest.review_id is not None
        assert latest.persona_skill == "pr-review"
        assert latest.persona_mtime_ns is not None
        assert latest.inline_comment_count == 1
        assert latest.summary_chars and latest.summary_chars > 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_system_prompt_carries_persona_and_json_schema(tmp_path: Path) -> None:
    """Per `contracts/claude-review-output.md` §2 the system prompt must be
    persona body + an explicit `Output ONLY a JSON object … JSON schema:` directive
    + the dumped ReviewOutput schema. Without this the model emits markdown and
    handler's `json.loads` fails with "Expecting value: line 1 column 1".
    """
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps({"verdict": "APPROVE", "summary": "ok at deadbeef", "comments": []})
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)

        assert factory.session.calls, "handler did not call Claude"
        system = factory.session.calls[0]["system"]
        assert isinstance(system, str)
        # Persona body intact
        assert _PERSONA_BODY in system
        # Directive verbatim from contract §2
        assert "Output ONLY a JSON object" in system
        assert "JSON schema:" in system
        # Dumped schema contains both top-level keys
        assert '"summary"' in system
        assert '"comments"' in system
        # Persona comes first; directive appended after
        assert system.index(_PERSONA_BODY) < system.index("Output ONLY a JSON object")
        # Slim-down invariants — these are the load-bearing rules the
        # directive enforces on Claude. If they drift, posted summaries
        # will regress to verbose English.
        assert "<= 1500 chars" in system
        assert "2500 chars" in system
        assert "Korean" in system
        assert "— daeyeon-bot 🐥" in system
        assert "InlineComment" in system
        assert "MUST NOT contain the sign-off marker" in system
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_skipped_self_authored(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "o/r",
        7,
        head_sha="deadbeef",
        author=fake_gh.user_login,  # operator authored the PR
        files=_FILES_ONE_FILE,
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh)
    try:
        event = _auto_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(FakeFactory(session=FakeClaudeSession())))
        assert isinstance(result, Ack)
        assert fake_gh.posted_reviews() == []
        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "skipped_self_authored"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_review_self_downgrades_approve_to_comment(tmp_path: Path) -> None:
    """`review_self=True` reviews the operator's own PR, posting COMMENT not APPROVE."""
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "o/r",
        7,
        head_sha="deadbeef",
        author=fake_gh.user_login,  # operator authored the PR
        requested=("someone-else",),  # operator is NOT its own reviewer
        files=_FILES_ONE_FILE,
    )
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {
                    "verdict": "APPROVE",
                    "summary": (
                        "**Verdict**: APPROVE — 모든 finding 0개.\n\n"
                        "**개요**\n변경사항은 작고 컨벤션을 따라간다.\n\n"
                        "— daeyeon-bot 🐥"
                    ),
                    "comments": [],
                }
            )
        )
    )
    handler, conn, _ = await _build_handler(
        tmp_path,
        fake_gh=fake_gh,
        factory=factory,
        config_overrides=PrReviewHandlerEntry(
            persona_skill="pr-review",
            min_persona_chars=50,
            size_budget=SizeBudget(max_lines=1000, max_files=50),
            review_self=True,
        ),
    )
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        posted = fake_gh.posted_reviews()
        assert len(posted) == 1
        # GitHub rejects a self-APPROVE → the verdict is posted as COMMENT.
        assert posted[0]["event"] == "COMMENT"
        assert posted[0]["comments"] == []
        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "posted"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_review_self_disabled_still_skips_own_pr(tmp_path: Path) -> None:
    """Default `review_self=False` keeps the `skipped_self_authored` gate."""
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "o/r",
        7,
        head_sha="deadbeef",
        author=fake_gh.user_login,
        requested=("someone-else",),
        files=_FILES_ONE_FILE,
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh)
    try:
        event = _auto_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(FakeFactory(session=FakeClaudeSession())))
        assert isinstance(result, Ack)
        assert fake_gh.posted_reviews() == []
        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "skipped_self_authored"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_skipped_withdrawn_when_pr_closed(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "o/r",
        7,
        head_sha="deadbeef",
        author="alice",
        files=_FILES_ONE_FILE,
        state="closed",
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh)
    try:
        event = _auto_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(FakeFactory(session=FakeClaudeSession())))
        assert isinstance(result, Ack)
        assert fake_gh.posted_reviews() == []
        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "skipped_withdrawn"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_skipped_when_username_not_in_requested(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "o/r",
        7,
        head_sha="deadbeef",
        author="alice",
        requested=("someone-else",),  # operator no longer requested
        files=_FILES_ONE_FILE,
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh)
    try:
        event = _auto_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(FakeFactory(session=FakeClaudeSession())))
        assert isinstance(result, Ack)
        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "skipped_withdrawn"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_skipped_when_repo_not_in_allowlist(tmp_path: Path) -> None:
    """Repo allowlist gate must fire before any `gh.pr_get` round-trip."""
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "evil-org/some-repo",
        7,
        head_sha="deadbeef",
        author="alice",
        files=_FILES_ONE_FILE,
    )
    handler, conn, _ = await _build_handler(
        tmp_path,
        fake_gh=fake_gh,
        config_overrides=PrReviewHandlerEntry(
            persona_skill="pr-review",
            min_persona_chars=50,
            allowed_repos=["rebellions-sw/*"],
        ),
    )
    try:
        event = make_event(
            type="gh.review_requested",  # auto path — allowlist applies
            payload={
                "repo": "evil-org/some-repo",
                "pr_number": 7,
                "head_sha": "deadbeef",
                "request_gen": 2,
                "force": True,  # force MUST NOT bypass the allowlist on auto events
            },
            created_at=datetime(2026, 5, 4, tzinfo=UTC),
        )
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(FakeFactory(session=FakeClaudeSession())))
        assert isinstance(result, Ack)
        # No GitHub round-trip should have happened — disallowed repo is gated
        # before `pr_get`. (Handler returns Ack immediately.)
        assert fake_gh.posted_reviews() == []
        latest = await find_latest(conn, "evil-org/some-repo", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "skipped_disallowed_repo"
        # Persona never loaded → audit row carries no persona metadata.
        assert latest.persona_skill is None
        assert latest.persona_mtime_ns is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_allowed_repo_proceeds_through_gate(tmp_path: Path) -> None:
    """A repo matching the allowlist must NOT be skipped by the gate.

    Verifies the gate is not a blanket short-circuit — a matching glob
    falls through to the normal review path.
    """
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "rebellions-sw/daeyeon-bot",
        7,
        head_sha="deadbeef",
        author="alice",
        files=_FILES_ONE_FILE,
    )
    factory = FakeFactory(
        session=FakeClaudeSession(default='{"verdict": "APPROVE", "summary": "ok", "comments": []}')
    )
    handler, conn, _ = await _build_handler(
        tmp_path,
        fake_gh=fake_gh,
        factory=factory,
        config_overrides=PrReviewHandlerEntry(
            persona_skill="pr-review",
            min_persona_chars=50,
            allowed_repos=["rebellions-sw/*"],
        ),
    )
    try:
        event = make_event(
            type="gh.review_requested",  # auto path — allowlist match must proceed
            payload={
                "repo": "rebellions-sw/daeyeon-bot",
                "pr_number": 7,
                "head_sha": "deadbeef",
                "request_gen": 1,
                "force": False,
            },
            created_at=datetime(2026, 5, 4, tzinfo=UTC),
        )
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        latest = await find_latest(conn, "rebellions-sw/daeyeon-bot", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "posted"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_manual_bypasses_disallowed_repo_and_withdrawn(tmp_path: Path) -> None:
    """An explicit `pr.review.manual` fire reviews a PR even when the repo is
    not in `allowed_repos` and the operator is not a requested reviewer."""
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "evil-org/some-repo",
        7,
        head_sha="deadbeef",
        author="alice",
        requested=("someone-else",),  # operator is NOT a requested reviewer
        files=_FILES_ONE_FILE,
    )
    factory = FakeFactory(
        session=FakeClaudeSession(default='{"verdict": "APPROVE", "summary": "ok", "comments": []}')
    )
    handler, conn, _ = await _build_handler(
        tmp_path,
        fake_gh=fake_gh,
        factory=factory,
        config_overrides=PrReviewHandlerEntry(
            persona_skill="pr-review",
            min_persona_chars=50,
            allowed_repos=["rebellions-sw/*"],  # does NOT include evil-org
        ),
    )
    try:
        event = _manual_event(repo="evil-org/some-repo")
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        posted = fake_gh.posted_reviews()
        assert len(posted) == 1  # reviewed despite disallowed repo + not-requested
        latest = await find_latest(conn, "evil-org/some-repo", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "posted"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_manual_reviews_own_pr_even_without_review_self(tmp_path: Path) -> None:
    """A manual fire reviews the operator's own PR (COMMENT) even when
    `review_self=False` — the explicit command overrides the self-skip."""
    fake_gh = FakeGh()
    fake_gh.add_pr(
        "rebellions-sw/daeyeon-bot",
        7,
        head_sha="deadbeef",
        author=fake_gh.user_login,  # operator's own PR
        requested=("someone-else",),
        files=_FILES_ONE_FILE,
    )
    factory = FakeFactory(
        session=FakeClaudeSession(default='{"verdict": "APPROVE", "summary": "ok", "comments": []}')
    )
    handler, conn, _ = await _build_handler(
        tmp_path,
        fake_gh=fake_gh,
        factory=factory,
        config_overrides=PrReviewHandlerEntry(
            persona_skill="pr-review",
            min_persona_chars=50,
            allowed_repos=["rebellions-sw/*"],
            review_self=False,  # auto path would skip; manual must not
        ),
    )
    try:
        event = _manual_event(repo="rebellions-sw/daeyeon-bot")
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        posted = fake_gh.posted_reviews()
        assert len(posted) == 1
        assert posted[0]["event"] == "COMMENT"  # self-APPROVE still downgraded
        latest = await find_latest(conn, "rebellions-sw/daeyeon-bot", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "posted"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_skipped_too_large_posts_templated_summary(tmp_path: Path) -> None:
    big_files = [
        {
            "filename": f"f{i}.py",
            "additions": 200,
            "deletions": 0,
            "status": "modified",
            "patch": _PATCH_HUNK_AT_5,
        }
        for i in range(10)
    ]
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=big_files)
    handler, conn, _ = await _build_handler(
        tmp_path,
        fake_gh=fake_gh,
        config_overrides=PrReviewHandlerEntry(
            persona_skill="pr-review",
            min_persona_chars=50,
            size_budget=SizeBudget(max_lines=500, max_files=50),
        ),
    )
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(FakeFactory(session=FakeClaudeSession())))
        assert isinstance(result, Ack)
        posted = fake_gh.posted_reviews()
        assert len(posted) == 1
        assert "too large for an automated review" in posted[0]["body"]
        assert "limit 500" in posted[0]["body"]
        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "skipped_too_large"
        assert latest.inline_comment_count == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_persona_unavailable_raises_validation_error(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    # Skip persona materialization → loader.load() raises.
    skills_root = tmp_path / "empty_skills"
    skills_root.mkdir()
    loader = PersonaLoader(skills_root=skills_root)
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    handler = PrReviewHandler(
        manifest=MANIFEST,
        gh=fake_gh,
        persona_loader=loader,
        config=PrReviewHandlerEntry(persona_skill="pr-review", min_persona_chars=50),
        github_username=fake_gh.user_login,
        db=conn,
    )
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        with pytest.raises(ValidationError):
            await handler.handle(event, _ctx(FakeFactory(session=FakeClaudeSession())))
        # An audit row records the failure so the operator can find it.
        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "failed"
        assert latest.error and "persona unavailable" in latest.error
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_claude_malformed_twice_raises_permanent(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            responses=["this is not JSON", "still not JSON"], default="not JSON"
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        with pytest.raises(PermanentError):
            await handler.handle(event, _ctx(factory))
        assert fake_gh.posted_reviews() == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_out_of_hunk_anchor_folded_into_summary(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {
                    "verdict": "PASS",
                    "summary": "Reviewed at deadbeef.",
                    "comments": [
                        {
                            "path": "src/foo.py",
                            "line": 6,  # in-hunk
                            "body": "in-hunk comment",
                        },
                        {
                            "path": "src/foo.py",
                            "line": 99,  # out-of-hunk → folded
                            "body": "out-of-hunk feedback",
                        },
                    ],
                }
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        posted = fake_gh.posted_reviews()
        assert len(posted) == 1
        assert len(posted[0]["comments"]) == 1
        assert posted[0]["comments"][0]["line"] == 6
        assert "out-of-hunk feedback" in posted[0]["body"]
        assert "near L99" in posted[0]["body"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_force_supersede_prepends_header_and_chains_audit(
    tmp_path: Path,
) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            responses=[
                json.dumps(
                    {"verdict": "APPROVE", "summary": "First pass at deadbeef.", "comments": []}
                ),
                json.dumps(
                    {"verdict": "APPROVE", "summary": "Second pass at deadbeef.", "comments": []}
                ),
            ],
            default="{}",
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        # Initial (non-force) review at SHA deadbeef.
        event_a = _manual_event()
        await _seed_event_row(conn, event_a)
        await handler.handle(event_a, _ctx(factory))

        # Force re-review at the same SHA.
        event_b = _manual_event(force=True)
        await _seed_event_row(conn, event_b)
        await handler.handle(event_b, _ctx(factory))

        posted = fake_gh.posted_reviews()
        assert len(posted) == 2
        # Supersede header is italicized markdown wrapping `_…_`, so the
        # SHA is enclosed in backticks; assert the parts independently.
        assert "Updated review for SHA" in posted[1]["body"]
        assert "deadbeef" in posted[1]["body"]
        assert "supersedes earlier bot review" in posted[1]["body"]

        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "posted"
        assert latest.review_id == posted[1]["review_id"]
        assert latest.superseded_review_ids == (posted[0]["review_id"],)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_redaction_in_summary_blocks_post(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    leaky_summary = (
        "Reviewed at deadbeef. Found leaked GitHub PAT: ghp_AAAAAAAAAAAAAAAAAAAAAAAAA in config."
    )
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps({"verdict": "APPROVE", "summary": leaky_summary, "comments": []})
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        with pytest.raises(PermanentError, match="redaction"):
            await handler.handle(event, _ctx(factory))
        assert fake_gh.posted_reviews() == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_redaction_in_inline_comment_blocks_post(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {
                    "verdict": "PASS",
                    "summary": "Reviewed at deadbeef. Spotted suspicious value below.",
                    "comments": [
                        {
                            "path": "src/foo.py",
                            "line": 6,
                            "body": ("Found token ghp_BBBBBBBBBBBBBBBBBBBBBBBBB — please remove."),
                        }
                    ],
                }
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        with pytest.raises(PermanentError, match="redaction"):
            await handler.handle(event, _ctx(factory))
        assert fake_gh.posted_reviews() == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_entropy_only_hit_in_summary_posts_unchanged(tmp_path: Path) -> None:
    """A4: entropy-only redaction hits must not block posting. The summary
    is posted as-is and a `pr_review.redaction_entropy` warning is emitted."""
    import secrets as stdlib_secrets

    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    high_entropy = stdlib_secrets.token_urlsafe(32)
    summary_with_entropy = (
        f"Reviewed at deadbeef. Saw an opaque identifier `{high_entropy}` in the test fixture."
    )
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {"verdict": "APPROVE", "summary": summary_with_entropy, "comments": []}
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        posted = fake_gh.posted_reviews()
        assert len(posted) == 1
        # Summary posted with the high-entropy token intact.
        assert high_entropy in posted[0]["body"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_clean_content_passes_redaction_and_posts(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {
                    "verdict": "PASS",
                    "summary": "Reviewed at deadbeef. Looks clean.",
                    "comments": [
                        {
                            "path": "src/foo.py",
                            "line": 6,
                            "body": "tiny nit: rename `x` to something readable.",
                        }
                    ],
                }
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        assert len(fake_gh.posted_reviews()) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_already_reviewed_skips_without_force(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {"verdict": "APPROVE", "summary": "Reviewed at deadbeef.", "comments": []}
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event_a = _manual_event()
        await _seed_event_row(conn, event_a)
        await handler.handle(event_a, _ctx(factory))
        assert len(fake_gh.posted_reviews()) == 1

        event_b = _manual_event()  # same head_sha, force=False
        await _seed_event_row(conn, event_b)
        result = await handler.handle(event_b, _ctx(factory))
        assert isinstance(result, Ack)
        # No second review posted; a new audit row recorded as
        # `skipped_already_reviewed`.
        assert len(fake_gh.posted_reviews()) == 1
        latest = await find_latest(conn, "o/r", 7, "deadbeef")
        assert latest is not None
        assert latest.status == "skipped_already_reviewed"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pause_guard_short_circuits_before_post(tmp_path: Path) -> None:
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {"verdict": "APPROVE", "summary": "Reviewed at deadbeef.", "comments": []}
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    from daeyeon_bot.core.errors import QuotaError

    async def _paused() -> None:
        raise QuotaError("paused")

    handler.pause_guard = _paused
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        with pytest.raises(QuotaError):
            await handler.handle(event, _ctx(factory))
        assert fake_gh.posted_reviews() == []
    finally:
        await conn.close()


# ── Phase A/B/C/D — verdict, GH event, prior reviews, persona ────────────────


@pytest.mark.asyncio
async def test_verdict_approve_emits_gh_approve_event(tmp_path: Path) -> None:
    """`verdict=APPROVE` with empty `comments[]` posts a GitHub APPROVE review."""
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {
                    "verdict": "APPROVE",
                    "summary": (
                        "**Verdict**: APPROVE — 모든 finding 0개.\n\n"
                        "**개요**\n변경사항은 작고 컨벤션을 따라간다.\n\n"
                        "— daeyeon-bot 🐥"
                    ),
                    "comments": [],
                }
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        posted = fake_gh.posted_reviews()
        assert len(posted) == 1
        assert posted[0]["event"] == "APPROVE"
        assert posted[0]["comments"] == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_verdict_pass_emits_gh_comment_event(tmp_path: Path) -> None:
    """`verdict=PASS` with MINOR-only comments posts a GitHub COMMENT review."""
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps(
                {
                    "verdict": "PASS",
                    "summary": (
                        "**Verdict**: PASS — MINOR 1개. 별도 PR 가능.\n\n"
                        "**개요**\n사소한 nit이 하나 있으나 머지 가능.\n\n"
                        "— daeyeon-bot 🐥"
                    ),
                    "comments": [
                        {
                            "path": "src/foo.py",
                            "line": 6,
                            "body": "[MINOR] src/foo.py:6 — 상수 네이밍 권장.",
                        }
                    ],
                }
            )
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        posted = fake_gh.posted_reviews()
        assert len(posted) == 1
        assert posted[0]["event"] == "COMMENT"
        assert len(posted[0]["comments"]) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_verdict_approve_with_comments_rejected_by_schema(tmp_path: Path) -> None:
    """Schema validator rejects `verdict=APPROVE` paired with non-empty `comments[]`."""
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    # Both attempts return the same inconsistent JSON — handler raises PermanentError.
    bad = json.dumps(
        {
            "verdict": "APPROVE",
            "summary": "Reviewed at deadbeef.",
            "comments": [{"path": "src/foo.py", "line": 6, "body": "nit"}],
        }
    )
    factory = FakeFactory(session=FakeClaudeSession(default=bad))
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        with pytest.raises(PermanentError, match="malformed review"):
            await handler.handle(event, _ctx(factory))
        # Nothing posted to GH on schema rejection.
        assert fake_gh.posted_reviews() == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_prior_reviews_threaded_into_user_message(tmp_path: Path) -> None:
    """Seeded prior reviews land in the user message under a `Prior reviews` section."""
    fake_gh = FakeGh()
    fake_gh.add_pr("o/r", 7, head_sha="deadbeef", author="alice", files=_FILES_ONE_FILE)
    fake_gh.seed_prior_reviews(
        [
            {
                "id": 111,
                "submitted_at": "2026-05-01T10:00:00Z",
                "commit_id": "cafebabecafebabe",
                "state": "COMMENTED",
                "body": "**Verdict**: CONCERNS — MAJOR 1개. `src/foo.py:6` 에서 ...",
                "inline_comments": [
                    {
                        "path": "src/foo.py",
                        "line": 6,
                        "body": "[MAJOR] src/foo.py:6 — 예전 round에서 지적한 점.",
                    }
                ],
            }
        ]
    )
    factory = FakeFactory(
        session=FakeClaudeSession(
            default=json.dumps({"verdict": "APPROVE", "summary": "ok at deadbeef", "comments": []})
        )
    )
    handler, conn, _ = await _build_handler(tmp_path, fake_gh=fake_gh, factory=factory)
    try:
        event = _manual_event()
        await _seed_event_row(conn, event)
        result = await handler.handle(event, _ctx(factory))
        assert isinstance(result, Ack)
        user_msg = factory.session.calls[0]["prompt"]
        assert isinstance(user_msg, str)
        assert "Prior reviews" in user_msg
        assert "Prior #1" in user_msg
        assert "cafebabe" in user_msg  # truncated SHA visible
        assert "src/foo.py:6" in user_msg  # inline comment rendered
    finally:
        await conn.close()


def test_output_directive_mentions_evidence_discipline() -> None:
    """The persona prompt must instruct: no hypothetical clauses, MINOR self-gate."""
    from daeyeon_bot.handlers.pr_review_prompt import OUTPUT_DIRECTIVE

    assert "추측 금지" in OUTPUT_DIRECTIVE or "no hypothetical" in OUTPUT_DIRECTIVE.lower()
    assert "MINOR" in OUTPUT_DIRECTIVE
    assert "APPROVE" in OUTPUT_DIRECTIVE


# Awaitable is re-exported below so pyright's "unused import" doesn't fire
# (the symbol is used only to type the FakeFactory's __call__ in some tests).
_AWAITABLE_NONE = Awaitable[None]
