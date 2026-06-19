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
from daeyeon_bot.infra.gh_cli import FailedJob
from daeyeon_bot.infra.oncall_wiki import WikiRefresh
from daeyeon_bot.infra.persona_loader import PersonaLoader
from daeyeon_bot.infra.slack import PostResult
from daeyeon_bot.infra.storage import apply_migrations, open_db
from tests.fakes.loki import FakeLokiClient

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
    annotations: str = ""
    jobs: list[Any] = field(default_factory=list)

    async def run_failed_job_logs(self, repo: str, run_id: str) -> str:
        if self.raise_unavailable:
            raise RunLogUnavailableError("could not find any workflow run")
        return self.log

    async def run_failed_annotations(self, repo: str, run_id: str) -> str:
        return self.annotations

    async def failed_jobs(self, repo: str, run_id: str) -> list[Any]:
        return self.jobs


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


def _make_handler(
    db: aiosqlite.Connection,
    *,
    slack: Any,
    gh: Any,
    wiki: Any,
    config: CiTriageHandlerEntry | None = None,
    loki: Any = None,
) -> CiTriageHandler:
    return CiTriageHandler(
        manifest=MANIFEST,
        slack=slack,
        gh=gh,
        oncall_wiki=wiki,
        persona_loader=PersonaLoader(skills_root=_persona_root()),
        config=config or CiTriageHandlerEntry(dry_run_channel="C_DRY", post_target="dry_run"),
        db=db,
        loki=loki,
    )


def _auto_event(channel: str, ts: str, run: str) -> Event:
    raw = (
        f"premerge / result failed — https://github.com/rebellions-sw/ssw-bundle/actions/runs/{run}"
    )
    return make_event(
        type="slack.ci_alert",
        payload={
            "channel_id": channel,
            "message_ts": ts,
            "author_id": "U069J27G2G6",
            "raw_blob": raw,
        },
        created_at=_NOW,
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
        assert "🤖 daeyeon-bot" in slack.posts[0]["text"]

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


# ── device-level Loki path (no run log → fwlog/kernel evidence) ──────────────


async def test_device_level_loki_evidence(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        # SSW-Alert-Bot device alert: host in [..] tag, NO actions/runs link → the
        # run-log path is skipped and the device-level Loki path supplies evidence.
        raw = (
            ":warning: *[ssw-smci-15] SMC FW update FAILED*\nresult 00000002, smc update has failed"
        )
        ev = make_event(
            type="slack.ci_alert",
            payload={
                "channel_id": "C1",
                "message_ts": "100.1",
                "author_id": "U09RJGLLPLZ",
                "raw_blob": raw,
            },
            created_at=_NOW,
        )
        await _seed_event(conn, ev, "d1")
        loki = FakeLokiClient()
        loki.set_response(
            "kernel",
            lines=(
                "[rbln-fwi] SMC update has failed 0x__6a0000",
                "device unreachable 0x50555746",
            ),
        )
        slack = _FakeSlack()
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki(), loki=loki)
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        # Loki was queried for the alert host on the kernel stream.
        assert loki.calls
        stream, logql, _start, _end = loki.calls[0]
        assert stream == "kernel"
        assert 'hostname="ssw-smci-15"' in logql
        assert len(slack.posts) == 1  # device-level triage still posts
    finally:
        await conn.close()


# ── P3: posting promotion ────────────────────────────────────────────────────


async def test_post_target_thread_replies_in_alert_thread(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        ev = _auto_event("C_ALERTS", "100.1", "27758520154")
        await _seed_event(conn, ev, "d1")
        slack = _FakeSlack()
        cfg = CiTriageHandlerEntry(dry_run_channel="C_DRY", post_target="thread")
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki(), config=cfg)
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        assert len(slack.posts) == 1
        # Posts to the ORIGINAL alert channel, as a thread reply on the alert ts.
        assert slack.posts[0]["channel"] == "C_ALERTS"
        assert slack.posts[0]["thread_ts"] == "100.1"
    finally:
        await conn.close()


async def test_force_supersede_prepends_header(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        # Prior posted triage for the same run.
        ev0 = _manual_event("rebellions-sw/ssw-bundle", "555")
        await _seed_event(conn, ev0, "d0")
        await insert_audit(
            conn,
            event_id=ev0.id,
            channel_id="manual:rebellions-sw/ssw-bundle",
            message_ts="555",
            status="posted",
            created_at=_NOW,
            repo="rebellions-sw/ssw-bundle",
            run_id="555",
        )
        await conn.commit()

        ev1 = _manual_event("rebellions-sw/ssw-bundle", "555", force=True)
        await _seed_event(conn, ev1, "d1")
        slack = _FakeSlack()
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki())
        result = await handler.handle(
            ev1, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        assert len(slack.posts) == 1  # force re-posts (does not skip)
        assert slack.posts[0]["text"].startswith("_Updated triage (supersedes")
    finally:
        await conn.close()


async def test_no_evidence_note_posted_when_enabled(tmp_path: Path) -> None:
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
        cfg = CiTriageHandlerEntry(
            dry_run_channel="C_DRY", post_target="dry_run", post_no_evidence_note=True
        )
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki(), config=cfg)
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        # A minimal note was posted (vs the default silent skip).
        assert len(slack.posts) == 1
        assert "수동 triage" in slack.posts[0]["text"]
        audit = await find_latest_for_message(conn, channel_id="C1", message_ts="100.1")
        assert audit is not None
        assert audit.status == "skipped_no_run_link"
    finally:
        await conn.close()


# ── DUT/host Loki 3-tier resolution (tier 2 log-host, tier 3 runner fallback) ─


async def test_loki_host_tier2_dut_from_job_log(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        ev = _auto_event("C_ALERTS", "100.1", "27800853109")
        await _seed_event(conn, ev, "d1")
        gh = _FakeGh(
            log="CR13-premerge-phase0 | SR-IOV VF on ssw-host-04 failed\nssw-host-04 -12 ENOMEM"
        )
        loki = FakeLokiClient()
        loki.set_response("kernel", lines=("rebellions rbln0: VF BAR reassign -12",))
        handler = _make_handler(conn, slack=_FakeSlack(), gh=gh, wiki=_FakeWiki(), loki=loki)
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        # Loki queried for the DUT host from the LOG (ssw-host-04), not a runner.
        assert loki.calls
        assert 'hostname="ssw-host-04"' in loki.calls[0][1]
    finally:
        await conn.close()


async def test_loki_host_tier3_runner_fallback(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        ev = _auto_event("C_ALERTS", "100.1", "27800853109")
        await _seed_event(conn, ev, "d1")
        # logs gone (404) but annotations carry the runner-death reason → don't skip
        gh = _FakeGh(
            raise_unavailable=True,
            annotations="[failure] atom-test: The self-hosted runner lost communication",
            jobs=[
                FailedJob(
                    "82272806287",
                    "ci-test",
                    "ssw-pc-21",
                    "2026-06-19T02:25:43Z",
                    "2026-06-19T02:42:45Z",
                )
            ],
        )
        loki = FakeLokiClient()
        loki.set_response("kernel", lines=("rebellions rbln0: reboot — uptime reset",))
        handler = _make_handler(conn, slack=_FakeSlack(), gh=gh, wiki=_FakeWiki(), loki=loki)
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        # No DUT host in the (empty) log → fall back to the runner host ssw-pc-21.
        assert loki.calls
        assert 'hostname="ssw-pc-21"' in loki.calls[0][1]
    finally:
        await conn.close()


def test_action_items_splits_circled_numerals() -> None:
    from daeyeon_bot.handlers.ci_triage import _action_items

    items = _action_items("① 첫째 단계. ② 둘째 단계 (mp, 1) 확인. ③ 셋째.")
    assert items == ["첫째 단계.", "둘째 단계 (mp, 1) 확인.", "셋째."]


def test_action_items_preserves_trailing_paren() -> None:
    from daeyeon_bot.handlers.ci_triage import _action_items

    items = _action_items("① SDOC 티켓 생성(infra_env / SysFw 소유)")
    assert items == ["SDOC 티켓 생성(infra_env / SysFw 소유)"]


def test_action_items_falls_back_to_whole_string() -> None:
    from daeyeon_bot.handlers.ci_triage import _action_items

    assert _action_items("just one action, no enumerator") == ["just one action, no enumerator"]


def test_render_slack_body_is_block_structured() -> None:
    from daeyeon_bot.core.ci_triage.types import ParsedAlert, RunRef, WikiMatch
    from daeyeon_bot.handlers.ci_triage import _render_slack_body
    from daeyeon_bot.handlers.ci_triage_schemas import Evidence, TriageOutput

    t = TriageOutput(
        attribution="infra_env",
        classification="device_failure",
        owner_area="SysFw",
        confidence="medium",
        summary="요약문.",
        likely_cause="원인.",
        known_remedy="복구법.",
        recommended_action="① 단계 하나. ② 단계 둘.",
        rerun_advice="needs_investigation",
        needs_human=True,
        log_evidence=(Evidence(quote="heartbeat: 0", citation="kernel/ssw-pc-21"),),
    )
    alert = ParsedAlert(
        channel_id="C1",
        message_ts="1.1",
        author_id=None,
        merged_text="",
        run_ref=RunRef(repo="rebellions-sw/ssw-bundle", run_id="42"),
    )
    wiki = [
        WikiMatch(
            path="wiki/oncall/incidents/foo-bar.md", signature_matched=True, score=3, snippet=""
        )
    ]
    out = _render_slack_body(alert, t, wiki)

    assert "*✅ 조치*" in out
    assert "\n1. 단계 하나." in out and "\n2. 단계 둘." in out  # numbered, one per line
    assert "↪️ *rerun*: needs_investigation" in out
    assert "*🧾 근거*" in out and "> heartbeat: 0  — kernel/ssw-pc-21" in out
    assert "• foo-bar.md" in out  # basename bullet, not full vault path
    assert "wiki/oncall/incidents/foo-bar.md" not in out
    assert "🤖 daeyeon-bot" in out
