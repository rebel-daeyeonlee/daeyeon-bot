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
        "headline": "QEMU golden-base 이미지 소실",
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
    # P1 cross-run: `meta` None → run_meta yields workflow_id=None → no comparison
    # (the default for tests that don't care). Set both to exercise the path.
    meta: Any = None
    workflow_runs: list[Any] = field(default_factory=list)

    async def run_failed_job_logs(self, repo: str, run_id: str) -> str:
        if self.raise_unavailable:
            raise RunLogUnavailableError("could not find any workflow run")
        return self.log

    async def run_failed_annotations(self, repo: str, run_id: str) -> str:
        return self.annotations

    async def failed_jobs(self, repo: str, run_id: str) -> list[Any]:
        return self.jobs

    async def run_meta(self, repo: str, run_id: str) -> Any:
        from daeyeon_bot.infra.gh_cli import WorkflowRunMeta

        if self.meta is not None:
            return self.meta
        return WorkflowRunMeta(
            run_id=run_id,
            workflow_id=None,
            head_sha=None,
            head_branch=None,
            event=None,
            run_attempt=None,
        )

    async def list_workflow_runs(
        self, repo: str, *, workflow_id: str, per_page: int = 30, branch: str | None = None
    ) -> list[Any]:
        return self.workflow_runs


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
    jira: Any = None,
    linear: Any = None,
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
        jira=jira,
        linear=linear,
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


async def test_cross_run_systemic_line_posted(tmp_path: Path) -> None:
    """P1: when other recent PRs of the same workflow also fail, the posted body
    carries the 🔬 systemic comparison line."""
    from daeyeon_bot.infra.gh_cli import RunSummary, WorkflowRunMeta

    def _run(sha: str, conclusion: str) -> RunSummary:
        return RunSummary(
            id=sha,
            head_sha=sha,
            head_branch="pr",
            status="completed",
            conclusion=conclusion,
            event="pull_request",
            created_at="2026-06-24T00:00:00Z",
        )

    conn = await _open(tmp_path)
    try:
        ev = _manual_event("rebellions-sw/ssw-bundle", "27758520154")
        await _seed_event(conn, ev, "dx")
        slack = _FakeSlack()
        gh = _FakeGh(
            meta=WorkflowRunMeta(
                run_id="27758520154",
                workflow_id="555",
                head_sha="mine",
                head_branch="pr-x",
                event="pull_request",
                run_attempt=1,
            ),
            workflow_runs=[
                _run("mine", "failure"),
                _run("o1", "failure"),
                _run("o2", "failure"),
                _run("o3", "failure"),
                _run("o4", "success"),
            ],
        )
        handler = _make_handler(conn, slack=slack, gh=gh, wiki=_FakeWiki())
        result = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(result, Ack)
        assert "🔬" in slack.posts[0]["text"]
        assert "환경·인프라 유력" in slack.posts[0]["text"]
    finally:
        await conn.close()


async def test_recurrence_line_when_signature_seen_before(tmp_path: Path) -> None:
    """P2: a prior posted triage with the same signature → 🔁 N회 line."""
    from daeyeon_bot.handlers.ci_triage import _failure_signature
    from daeyeon_bot.infra.ci_triage_audit import insert_audit

    conn = await _open(tmp_path)
    try:
        ev = _manual_event("rebellions-sw/ssw-bundle", "27758520154")
        await _seed_event(conn, ev, "dr")
        # a prior posted row carrying the signature this triage will compute.
        sig = _failure_signature("QEMU golden-base 이미지 소실", "environment")
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at) VALUES"
            " ('prev', 'slack.ci_alert', 1, 'x', 'dprev', '{}', 'tr', '2026-06-18T00:00:00Z')"
        )
        await conn.commit()
        await insert_audit(
            conn,
            event_id="prev",
            channel_id="C9",
            message_ts="111.1",
            status="posted",
            created_at=_NOW,
            signature=sig,
        )
        await conn.commit()
        slack = _FakeSlack()
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki())
        await handler.handle(ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE]))))
        text = slack.posts[0]["text"]
        assert "🔁" in text and "2회" in text
    finally:
        await conn.close()


class _FakeJiraIssue:
    def __init__(self, key: str, status: str) -> None:
        self.key = key
        self.status_name = status


class _FakeJiraPage:
    def __init__(self, issues: list[_FakeJiraIssue]) -> None:
        self.issues = tuple(issues)


@dataclass(slots=True)
class _FakeJira:
    base_url: str = "https://rbln.atlassian.net"

    async def search_jql(self, *, jql: str, fields: list[str], max_results: int = 2) -> Any:
        return _FakeJiraPage([_FakeJiraIssue("SSWCI-17228", "Triage")])


@dataclass(slots=True)
class _FakeLinear:
    async def search_issues(self, term: str, *, limit: int = 3) -> list[Any]:
        from daeyeon_bot.infra.linear_client import LinearIssue

        return [LinearIssue("DOLIN-2207", "fix", "u", "In Progress", "started")]


async def test_ticket_search_lines_posted(tmp_path: Path) -> None:
    """P2/P4: open Jira + Linear matches render as the 🎫 line when enabled."""
    conn = await _open(tmp_path)
    try:
        ev = _manual_event("rebellions-sw/ssw-bundle", "27758520154")
        await _seed_event(conn, ev, "dt")
        slack = _FakeSlack()
        cfg = CiTriageHandlerEntry(
            dry_run_channel="C_DRY", post_target="dry_run", ticket_search_enabled=True
        )
        handler = _make_handler(
            conn,
            slack=slack,
            gh=_FakeGh(),
            wiki=_FakeWiki(),
            config=cfg,
            jira=_FakeJira(),
            linear=_FakeLinear(),
        )
        await handler.handle(ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE]))))
        text = slack.posts[0]["text"]
        assert "🎫" in text
        # clickable Slack links: <url|KEY (status)>
        assert "<https://rbln.atlassian.net/browse/SSWCI-17228|SSWCI-17228 (Triage)>" in text
        assert "<u|DOLIN-2207 (In Progress)>" in text
    finally:
        await conn.close()


def test_failure_signature_is_host_agnostic() -> None:
    from daeyeon_bot.handlers.ci_triage import _failure_signature

    a = _failure_signature("ssw-smci-16 IOMMU IOTLB_INV_TIMEOUT", "device_failure")
    b = _failure_signature("ssw-host-04 IOMMU IOTLB_INV_TIMEOUT", "device_failure")
    assert a == b  # hostnames masked → same signature across hosts
    assert a.startswith("device_failure:") and "iommu" in a
    # a different failure class is a different signature
    assert _failure_signature("runfile install fail", "build_failure") != a


async def test_log_only_triage_posts_without_run_link(tmp_path: Path) -> None:
    """P3: a human post with a fenced failure log but no run link is triaged
    from the pasted log (degraded), not skipped."""
    raw = (
        "runfile 설치시 error message 확인\n"
        "```\n"
        "[2026-06-24 08:41:02] Devices ready: 8/16, waiting... (115s/120s)\n"
        "[2026-06-24 08:41:02] ERROR: Timeout: only 8/16 devices ready after 120s\n"
        "[2026-06-24 08:41:02] ERROR: Not all devices are ready after module load\n"
        "```"
    )
    ev = make_event(
        type="slack.ci_alert",
        payload={"channel_id": "C1", "message_ts": "5.5", "author_id": "UHUMAN", "raw_blob": raw},
        created_at=_NOW,
    )
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, ev, "dlo")
        slack = _FakeSlack()
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki())
        res = await handler.handle(
            ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE])))
        )
        assert isinstance(res, Ack)
        assert len(slack.posts) == 1  # degraded triage posted, not skipped
        audit = await find_latest_for_message(conn, channel_id="C1", message_ts="5.5")
        assert audit is not None and audit.status == "posted"
    finally:
        await conn.close()


async def test_log_only_disabled_skips(tmp_path: Path) -> None:
    raw = "fail\n```\nERROR: Timeout after 120s\nERROR: not ready\nexit code 1\n```"
    ev = make_event(
        type="slack.ci_alert",
        payload={"channel_id": "C1", "message_ts": "6.6", "author_id": "UHUMAN", "raw_blob": raw},
        created_at=_NOW,
    )
    conn = await _open(tmp_path)
    try:
        await _seed_event(conn, ev, "dlo2")
        slack = _FakeSlack()
        cfg = CiTriageHandlerEntry(
            dry_run_channel="C_DRY", post_target="dry_run", log_only_triage_enabled=False
        )
        handler = _make_handler(conn, slack=slack, gh=_FakeGh(), wiki=_FakeWiki(), config=cfg)
        await handler.handle(ev, _FakeCtx(FakeFactory(FakeClaudeSession(responses=[_GOOD_TRIAGE]))))
        assert slack.posts == []  # skipped, no evidence
        audit = await find_latest_for_message(conn, channel_id="C1", message_ts="6.6")
        assert audit is not None and audit.status == "skipped_no_run_link"
    finally:
        await conn.close()


def test_ticket_draft_only_for_confident_infra_without_tickets() -> None:
    from daeyeon_bot.handlers.ci_triage import _ticket_draft

    _, t, _ = _render_fixture()  # infra_env, medium
    assert _ticket_draft(t, [], enabled=True) is not None
    assert _ticket_draft(t, ["SSWCI-1 (open)"], enabled=True) is None  # has a match → no draft
    assert _ticket_draft(t, [], enabled=False) is None
    low = t.model_copy(update={"confidence": "low"})
    assert _ticket_draft(low, [], enabled=True) is None  # not confident
    reg = t.model_copy(update={"attribution": "product_regression"})
    assert _ticket_draft(reg, [], enabled=True) is None  # not infra_env


def test_render_shows_ticket_draft_when_no_tickets() -> None:
    from daeyeon_bot.handlers.ci_triage import _render_slack_body

    alert, t, wiki = _render_fixture()
    out = _render_slack_body(
        alert, t, wiki, ticket_draft='🆕 신규 SSWCI bug 제안: "x" (infra_env/SysFw)'
    )
    assert "🆕 신규 SSWCI bug 제안" in out
    # tickets present → draft suppressed
    out2 = _render_slack_body(alert, t, wiki, tickets=["SSWCI-9 (open)"], ticket_draft="🆕 nope")
    assert "🆕" not in out2 and "🎫 SSWCI-9" in out2


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


def _render_fixture() -> tuple[Any, Any, list[Any]]:
    from daeyeon_bot.core.ci_triage.types import ParsedAlert, RunRef, WikiMatch
    from daeyeon_bot.handlers.ci_triage_schemas import Evidence, TriageOutput

    t = TriageOutput(
        attribution="infra_env",
        classification="device_failure",
        owner_area="SysFw",
        confidence="medium",
        headline="ssw-pc-21 heartbeat 0",
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
        pr_number=3539,
    )
    wiki = [
        WikiMatch(
            path="wiki/oncall/incidents/foo-bar.md", signature_matched=True, score=3, snippet=""
        )
    ]
    return alert, t, wiki


def test_render_slack_body_terse_when_confident() -> None:
    """A medium/high-confidence call is terse: verdict head + decision + footer,
    no 요약/추정원인/근거 block, single top action only."""
    from daeyeon_bot.handlers.ci_triage import _render_slack_body

    alert, t, wiki = _render_fixture()
    out = _render_slack_body(alert, t, wiki)

    assert out.startswith("🔧 *infra_env* (medium) · ssw-pc-21 heartbeat 0")
    assert "↪️ *rerun 보류* — 단계 하나." in out  # rerun verdict + top action only
    assert "단계 둘." not in out  # secondary action hidden when confident
    assert "📋" not in out and "🧾" not in out  # no detail block
    # one-line footer: run link as <url|repo #PR>, no separate jobs line
    assert (
        "🔗 <https://github.com/rebellions-sw/ssw-bundle/actions/runs/42|ssw-bundle #3539>" in out
    )
    assert "🤖 daeyeon-bot" in out


def test_render_slack_body_detailed_when_low_confidence() -> None:
    """A low-confidence call appends the detail block (evidence + summary) and
    lists secondary actions, because on-call must investigate by hand."""
    from daeyeon_bot.handlers.ci_triage import _render_slack_body

    alert, t, wiki = _render_fixture()
    t = t.model_copy(update={"confidence": "low"})
    out = _render_slack_body(alert, t, wiki)

    assert "📋 요약문." in out
    assert "🧾 `heartbeat: 0` — kernel/ssw-pc-21" in out
    assert "• 단계 둘." in out  # secondary action shown in detail mode
    assert "foo-bar.md" in out  # wiki basename, not full vault path
    assert "wiki/oncall/incidents/foo-bar.md" not in out


def test_render_slack_body_context_lines() -> None:
    """Cross-run (P1) + recurrence/tickets (P2/P4) render as compact context
    lines when supplied."""
    from daeyeon_bot.handlers.ci_triage import _render_slack_body

    alert, t, wiki = _render_fixture()
    out = _render_slack_body(
        alert,
        t,
        wiki,
        cross_run="🔬 동일 host 최근 5 run 4 fail (PR 무관)",
        recurrence="🔁 7일 3회",
        tickets=["SSWCI-17228 (open)"],
    )

    assert "🔬 동일 host 최근 5 run 4 fail (PR 무관) · 🔁 7일 3회" in out
    assert "🎫 SSWCI-17228 (open)" in out
