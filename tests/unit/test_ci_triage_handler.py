"""ci_triage handler P1 paths — manual fire, skips, dedup (feature 003)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from daeyeon_bot.app.config import CiTriageHandlerEntry
from daeyeon_bot.core.ci_triage.types import WikiMatch
from daeyeon_bot.core.errors import RunLogUnavailableError
from daeyeon_bot.core.events import Event, make_event
from daeyeon_bot.core.results import Ack
from daeyeon_bot.handlers.ci_triage import MANIFEST, CiTriageHandler
from daeyeon_bot.infra.ci_triage_audit import find_latest_for_message, insert_audit
from daeyeon_bot.infra.claude import FakeClaudeSession, FakeFactory
from daeyeon_bot.infra.oncall_wiki import WikiRefresh
from daeyeon_bot.infra.persona_loader import PersonaLoader
from daeyeon_bot.infra.slack import PostResult
from daeyeon_bot.infra.storage import apply_migrations, open_db

_NOW = datetime(2026, 6, 19, 7, 0, 0, tzinfo=UTC)

_GOOD_TRIAGE = json.dumps(
    {
        "attribution": "infra_env",
        "classification": "environment",
        "owner_area": "DevOps",
        "confidence": "medium",
        "summary": "QEMU golden base 이미지 소실로 phase1 차단",
        "log_evidence": [
            {"quote": "rsync ... golden-base failed: No such file", "citation": "premerge / result"}
        ],
        "wiki_matches": [
            {
                "path": "wiki/oncall/incidents/qemu-golden-base-image-missing.md",
                "why": "signature match",
            }
        ],
        "likely_cause": "golden base image deleted from NFS",
        "known_remedy": "rebuild golden base image",
        "recommended_action": "golden image 재빌드 후 rerun",
        "rerun_advice": "needs_investigation",
        "needs_human": True,
    }
)


@dataclass(slots=True)
class _FakeCtx:
    claude_session_factory: Any
    trace_id: str = "trace-test"
    clock: Any = None

    def __post_init__(self) -> None:
        if self.clock is None:

            class _Clk:
                def now(self) -> datetime:
                    return _NOW

            self.clock = _Clk()


@dataclass(slots=True)
class _FakeSlack:
    posts: list[dict[str, Any]] = field(default_factory=list)

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        username: str | None = None,
        icon_emoji: str | None = None,
    ) -> PostResult:
        self.posts.append(
            {"channel": channel_id, "text": text, "thread_ts": thread_ts, "username": username}
        )
        return PostResult(channel=channel_id, ts="200.5")


@dataclass(slots=True)
class _FakeGh:
    log: str = "premerge / result | rsync ... golden-base failed: No such file\n##[error]Process completed with exit code 1."
    raise_unavailable: bool = False

    async def run_view_log_failed(self, repo: str, run_id: str) -> str:
        if self.raise_unavailable:
            raise RunLogUnavailableError("could not find any workflow run")
        return self.log


@dataclass(slots=True)
class _FakeWiki:
    matches: list[WikiMatch] = field(default_factory=list)
    available: bool = True

    async def ensure_fresh(self) -> WikiRefresh:
        return WikiRefresh(available=self.available, stale=False)

    async def search(
        self, *, signatures: tuple[str, ...], phrases: tuple[str, ...]
    ) -> list[WikiMatch]:
        return self.matches


def _persona_root() -> Path:
    return Path(__file__).resolve().parents[2] / ".claude" / "skills"


def _make_handler(db: aiosqlite.Connection, *, slack: Any, gh: Any, wiki: Any) -> CiTriageHandler:
    return CiTriageHandler(
        manifest=MANIFEST,
        slack=slack,
        gh=gh,
        oncall_wiki=wiki,
        persona_loader=PersonaLoader(skills_root=_persona_root()),
        config=CiTriageHandlerEntry(dry_run_channel="C_DRY", post_target="dry_run"),
        db=db,
    )


def _manual_event(repo: str, run_id: str, *, force: bool = False) -> Event:
    return make_event(
        type="ci.triage.manual",
        payload={"repo": repo, "run_id": run_id, "force": force},
        created_at=_NOW,
    )


async def _seed_event(conn: aiosqlite.Connection, event: Event, dedup: str) -> None:
    await conn.execute(
        "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
        " payload_json, trace_id, created_at)"
        " VALUES (?, ?, 1, 'manual', ?, '{}', 'tr', '2026-06-19T00:00:00Z')",
        (event.id, event.type, dedup),
    )
    await conn.commit()


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


async def test_manual_fire_happy_path_posts_dry_run(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        ev = _manual_event("rebellions-sw/ssw-bundle", "27758520154")
        await _seed_event(conn, ev, "d1")
        slack = _FakeSlack()
        wiki = _FakeWiki(
            matches=[
                WikiMatch(
                    path="wiki/oncall/incidents/qemu-golden-base-image-missing.md",
                    signature_matched=True,
                    score=30,
                    snippet="VM creation failed",
                )
            ]
        )
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=wiki)
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        assert len(slack.posts) == 1
        assert slack.posts[0]["channel"] == "C_DRY"
        assert slack.posts[0]["thread_ts"] is None
        assert "infra_env" in slack.posts[0]["text"]
        assert "🤖 automated first-pass" in slack.posts[0]["text"]

        audit = await find_latest_for_message(
            conn, channel_id="manual:rebellions-sw/ssw-bundle", message_ts="27758520154"
        )
        assert audit is not None
        assert audit.status == "posted"
        assert audit.attribution == "infra_env"
    finally:
        await conn.close()


async def test_run_log_unavailable_skips(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        ev = _manual_event("rebellions-sw/ssw-bundle", "999")
        await _seed_event(conn, ev, "d1")
        slack = _FakeSlack()
        handler = _make_handler(
            conn, slack=slack, gh=_FakeGh(raise_unavailable=True), wiki=_FakeWiki()
        )
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        assert slack.posts == []
        audit = await find_latest_for_message(
            conn, channel_id="manual:rebellions-sw/ssw-bundle", message_ts="999"
        )
        assert audit is not None
        assert audit.status == "skipped_log_unavailable"
    finally:
        await conn.close()


async def test_already_triaged_run_skips(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        # Prior posted audit for the same run.
        ev0 = _manual_event("rebellions-sw/ssw-bundle", "555")
        await _seed_event(conn, ev0, "d0")
        await insert_audit(
            conn,
            event_id=ev0.id,
            channel_id="manual:rebellions-sw/ssw-bundle",
            message_ts="prior",
            status="posted",
            created_at=_NOW,
            repo="rebellions-sw/ssw-bundle",
            run_id="555",
        )
        await conn.commit()

        ev1 = _manual_event("rebellions-sw/ssw-bundle", "555")
        await _seed_event(conn, ev1, "d1")
        slack = _FakeSlack()
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki())
        result = await handler.handle(
            ev1, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        assert slack.posts == []
        audit = await find_latest_for_message(
            conn, channel_id="manual:rebellions-sw/ssw-bundle", message_ts="555"
        )
        assert audit is not None
        assert audit.status == "skipped_already_triaged"
    finally:
        await conn.close()


async def test_no_run_link_skips(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        ev = make_event(
            type="slack.ci_alert",
            payload={
                "channel_id": "C1",
                "message_ts": "100.1",
                "raw_blob": "human chatter no link",
            },
            created_at=_NOW,
        )
        await _seed_event(conn, ev, "d1")
        slack = _FakeSlack()
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki())
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        assert slack.posts == []
        audit = await find_latest_for_message(conn, channel_id="C1", message_ts="100.1")
        assert audit is not None
        assert audit.status == "skipped_no_run_link"
    finally:
        await conn.close()
