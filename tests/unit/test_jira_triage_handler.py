"""Jira triage handler — T044 tests.

Covers the key skip/post paths via FakeJira + FakeLoki + FakeSshLogs
+ FakeClaudeSession + a tmp_path ssw-bundle fixture + tmp_path SQLite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from daeyeon_bot.app.config import JiraTriageHandlerEntry, LokiConfig
from daeyeon_bot.core.errors import PermanentError
from daeyeon_bot.core.events import Event, make_event
from daeyeon_bot.core.jira_triage.types import LokiSlice, RunSnapshot
from daeyeon_bot.core.results import Ack
from daeyeon_bot.handlers import jira_triage as _jt_module
from daeyeon_bot.handlers.jira_triage import (
    MANIFEST,
    JiraTriageHandler,
)
from daeyeon_bot.infra.claude import FakeClaudeSession, FakeFactory
from daeyeon_bot.infra.host_resolver import HostResolver
from daeyeon_bot.infra.jira_client import FieldDiscovery, JiraIdentity
from daeyeon_bot.infra.jira_triage_audit import find_latest
from daeyeon_bot.infra.persona_loader import PersonaLoader
from daeyeon_bot.infra.ssw_bundle import SswBundleClient
from daeyeon_bot.infra.storage import apply_migrations, open_db
from tests.fakes.jira_client import FakeJiraClient
from tests.fakes.loki import FakeLokiClient
from tests.fakes.ssh_logs import FakeSshLogClient
from tests.fakes.ssw_bundle_fixture import build_fixture

# Private helpers — tested via module attribute access (pyright-friendly).
_strip_code_fence = _jt_module._strip_code_fence  # pyright: ignore[reportPrivateUsage]
_verify_evidence_quotes = _jt_module._verify_evidence_quotes  # pyright: ignore[reportPrivateUsage]

# ── Helpers ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _FakeCtx:
    """HandlerContext stub — only fields the handler reads."""

    claude_session_factory: Any
    trace_id: str = "trace-test"
    clock: Any = None

    def __post_init__(self) -> None:
        if self.clock is None:

            class _Clk:
                def now(self) -> datetime:
                    return datetime(2026, 5, 13, 7, 0, 0, tzinfo=UTC)

            self.clock = _Clk()


def _bundled_persona_root() -> Path:
    """Path to the repo-bundled persona, used for the handler under test."""
    return Path(__file__).resolve().parents[2] / ".claude" / "skills"


def _make_handler(
    *,
    db: aiosqlite.Connection,
    jira: Any,
    loki: Any,
    ssh: Any,
    ssw_bundle: SswBundleClient,
    config: JiraTriageHandlerEntry | None = None,
    persona_root: Path | None = None,
) -> JiraTriageHandler:
    cfg = config or JiraTriageHandlerEntry(
        allowed_projects=["SSWCI"],
        persona_skill="daeyeon-bot-jira-triage",
        timeout_seconds=600,
    )
    persona_loader = PersonaLoader(skills_root=persona_root or _bundled_persona_root())
    return JiraTriageHandler(
        manifest=MANIFEST,
        jira=jira,
        loki=loki,
        ssh=ssh,
        ssw_bundle=ssw_bundle,
        host_resolver_factory=HostResolver,
        persona_loader=persona_loader,
        config=cfg,
        loki_config=LokiConfig(),
        db=db,
        jira_identity=JiraIdentity(
            account_id="557058:fake",
            email_address="daeyeon.lee@rebellions.ai",
            display_name="daeyeon",
        ),
        field_discovery=FieldDiscovery(
            branch_field_id="customfield_10042",
            commit_field_id="customfield_10043",
            team_field_id="",
            issuetype_name="Bug",
        ),
    )


# (unused stub removed; happy-path test inlines its own response.)


def _auto_event(issue_key: str = "SSWCI-100") -> Event:
    return make_event(
        type="jira.assigned",
        payload={
            "issue_key": issue_key,
            "project": "SSWCI",
            "assignment_gen": 1,
            "assignee_path": "user",
            "observed_at": "2026-05-13T07:00:00Z",
        },
        created_at=datetime(2026, 5, 13, 7, 0, 0, tzinfo=UTC),
    )


def _manual_event(issue_key: str, *, force: bool) -> Event:
    return make_event(
        type="jira.triage.manual",
        payload={
            "issue_key": issue_key,
            "force": force,
            "comment_seq": "manual_123" if force else "1",
        },
        created_at=datetime(2026, 5, 13, 7, 0, 0, tzinfo=UTC),
    )


async def _open(tmp_path: Path) -> aiosqlite.Connection:
    conn = await open_db(tmp_path / "state.db")
    await apply_migrations(conn)
    return conn


def _seed_regression_ticket(
    jira: Any,
    *,
    key: str = "SSWCI-100",
    epic_key: str = "SSWCI-99",
    branch: str = "release/v3.2",
    commit_sha: str | None = None,
    body: str | None = None,
) -> None:
    """Add a regression-failure ticket + its parent Epic to FakeJira."""
    if body is None:
        body = (
            "Start: 2026-05-13 06:54:48.924242\n"
            "End: 2026-05-13 07:07:38.172125\n"
            "ssh://automation@ssw-giga-02:"
            "/mnt/data/logs/regression-test/25746526668-1/ssw-giga-02/"
            "TC-0033-Dram_test_with_exception\n"
            "{noformat}\nstack trace here\n{noformat}"
        )
    jira.add_issue(
        key=key,
        summary="regression-test . ssw-giga-02 . TC-0033-Dram_test_with_exception",
        project="SSWCI",
        parent_key=epic_key,
        description_text=body,
    )
    jira.add_issue(
        key=epic_key,
        summary="Epic for the run",
        project="SSWCI",
        issuetype_name="Epic",
        custom_fields={
            "customfield_10042": branch,
            "customfield_10043": commit_sha or ("a" * 40),
        },
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_title_regex_miss_audit_skipped(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        jira = FakeJiraClient()
        jira.add_issue(
            key="SSWCI-200",
            summary="Some random ticket title",
            project="SSWCI",
        )
        fixture = build_fixture(tmp_path)
        ssw = SswBundleClient(
            clone_path=tmp_path / "var" / "ssw-bundle",
            remote_url=fixture.bundle_remote_url,
            project_root=tmp_path,
        )
        handler = _make_handler(
            db=conn,
            jira=jira,
            loki=FakeLokiClient(),
            ssh=FakeSshLogClient(),
            ssw_bundle=ssw,
        )
        # No event-row dependency for this branch — but audit needs an FK.
        event = _auto_event("SSWCI-200")
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES (?,?,1,?,?,'{}','tr','2026-05-13T00:00:00Z')",
            (event.id, event.type, "jira_assigned", "dedup-1"),
        )
        await conn.commit()
        result = await handler.handle(
            event, _FakeCtx(claude_session_factory=FakeFactory(FakeClaudeSession()))
        )
        assert isinstance(result, Ack)
        row = await find_latest(conn, "SSWCI-200")
        assert row is not None
        assert row.status == "skipped_not_regression_failure"
        assert jira.posted_comments() == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_disallowed_project_audit_skipped(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        jira = FakeJiraClient()
        # No issue added — the project gate fires before issue_get.
        fixture = build_fixture(tmp_path)
        ssw = SswBundleClient(
            clone_path=tmp_path / "var" / "ssw-bundle",
            remote_url=fixture.bundle_remote_url,
            project_root=tmp_path,
        )
        handler = _make_handler(
            db=conn,
            jira=jira,
            loki=FakeLokiClient(),
            ssh=FakeSshLogClient(),
            ssw_bundle=ssw,
        )
        event = _auto_event("OTHER-1")
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES (?,?,1,?,?,'{}','tr','2026-05-13T00:00:00Z')",
            (event.id, event.type, "jira_assigned", "dedup-x"),
        )
        await conn.commit()
        result = await handler.handle(
            event, _FakeCtx(claude_session_factory=FakeFactory(FakeClaudeSession()))
        )
        assert isinstance(result, Ack)
        row = await find_latest(conn, "OTHER-1")
        assert row is not None
        assert row.status == "skipped_not_regression_failure"
    finally:
        await conn.close()


def test_parse_epic_description_extracts_branch_and_commit() -> None:
    """SSWCI Epics put branch/commit in description wiki markup, not custom fields."""
    from daeyeon_bot.handlers import jira_triage as _jt

    _parse_epic_description = _jt._parse_epic_description  # pyright: ignore[reportPrivateUsage]

    text = (
        "*Branch*: release/v3.2\n"
        "*Commit*: 140112e9203598c72f568501eecac706cc125dcf\n"
        "*Host*: ssw-giga-02\n"
    )
    out = _parse_epic_description(text)
    assert out["branch"] == "release/v3.2"
    assert out["commit"] == "140112e9203598c72f568501eecac706cc125dcf"


def test_parse_epic_description_handles_missing_fields() -> None:
    from daeyeon_bot.handlers import jira_triage as _jt

    _parse_epic_description = _jt._parse_epic_description  # pyright: ignore[reportPrivateUsage]

    assert _parse_epic_description("") == {}
    assert _parse_epic_description("nothing here") == {}
    only_branch = _parse_epic_description("*Branch*: release/v3.2")
    assert only_branch == {"branch": "release/v3.2"}


@pytest.mark.asyncio
async def test_missing_epic_branch_audit_skipped(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        jira = FakeJiraClient()
        jira.add_issue(
            key="SSWCI-300",
            summary="regression-test . ssw-giga-02 . TC-1-x",
            project="SSWCI",
            parent_key="SSWCI-299",
        )
        jira.add_issue(
            key="SSWCI-299",
            summary="Epic",
            project="SSWCI",
            issuetype_name="Epic",
            custom_fields={
                # Branch missing, only commit present.
                "customfield_10043": "a" * 40,
            },
        )
        fixture = build_fixture(tmp_path)
        ssw = SswBundleClient(
            clone_path=tmp_path / "var" / "ssw-bundle",
            remote_url=fixture.bundle_remote_url,
            project_root=tmp_path,
        )
        handler = _make_handler(
            db=conn,
            jira=jira,
            loki=FakeLokiClient(),
            ssh=FakeSshLogClient(),
            ssw_bundle=ssw,
        )
        event = _auto_event("SSWCI-300")
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES (?,?,1,?,?,'{}','tr','2026-05-13T00:00:00Z')",
            (event.id, event.type, "jira_assigned", "dedup-m"),
        )
        await conn.commit()
        result = await handler.handle(
            event, _FakeCtx(claude_session_factory=FakeFactory(FakeClaudeSession()))
        )
        assert isinstance(result, Ack)
        row = await find_latest(conn, "SSWCI-300")
        assert row is not None
        assert row.status == "skipped_missing_metadata"
        assert "branch" in row.missing_fields
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_happy_path_posts_comment_and_audits(tmp_path: Path) -> None:
    conn = await _open(tmp_path)
    try:
        jira = FakeJiraClient()
        fixture = build_fixture(tmp_path)
        _seed_regression_ticket(jira, commit_sha=fixture.main_commit)
        loki = FakeLokiClient()
        # Snapshot must contain the quote verbatim — set up Loki kernel slice.
        loki.set_response("kernel", lines=("TDR detected line",))
        loki.set_response("fwlog", lines=())
        loki.set_response("smclog", lines=())
        loki.set_response("syslog", lines=())
        ssh = FakeSshLogClient()
        # Provide an SSH dump dir with output.xml so the SSH stage isn't an error.
        ssh.add_file(
            host="ssw-giga-02",
            remote_path="/mnt/data/logs/regression-test/25746526668-1/ssw-giga-02/TC-0033-Dram_test_with_exception",
            filename="output.xml",
            contents=b"<?xml ?>",
        )
        ssw = SswBundleClient(
            clone_path=tmp_path / "var" / "ssw-bundle",
            remote_url=fixture.bundle_remote_url,
            project_root=tmp_path,
        )
        # Override Claude to return a response whose evidence quote is in the snapshot.
        good = json.dumps(
            {
                "summary_md": "h3. Symptom\nx\n\nh3. Evidence cited\n- a",
                "domain": "CpFw",
                "severity": "sev2",
                "suspected_duplicates": [],
                "needs_human": False,
                "evidence": [
                    {
                        "source": "loki.kernel",
                        "quote": "TDR detected line",
                        "citation": "2026-05-13T06:55:12Z",
                    }
                ],
            }
        )
        session = FakeClaudeSession(responses=[good])
        handler = _make_handler(
            db=conn,
            jira=jira,
            loki=loki,
            ssh=ssh,
            ssw_bundle=ssw,
        )
        event = _auto_event("SSWCI-100")
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES (?,?,1,?,?,'{}','tr','2026-05-13T00:00:00Z')",
            (event.id, event.type, "jira_assigned", "dedup-h"),
        )
        await conn.commit()
        result = await handler.handle(event, _FakeCtx(claude_session_factory=FakeFactory(session)))
        assert isinstance(result, Ack)
        assert len(jira.posted_comments()) == 1
        row = await find_latest(conn, "SSWCI-100")
        assert row is not None
        assert row.status == "posted"
        assert row.domain == "CpFw"
        assert row.comment_id is not None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_already_triaged_force_false_skips(tmp_path: Path) -> None:
    """A prior 'posted' audit row + non-force event → skipped_already_triaged."""
    from daeyeon_bot.infra.jira_triage_audit import insert_audit

    conn = await _open(tmp_path)
    try:
        jira = FakeJiraClient()
        fixture = build_fixture(tmp_path)
        _seed_regression_ticket(jira, commit_sha=fixture.main_commit)
        ssw = SswBundleClient(
            clone_path=tmp_path / "var" / "ssw-bundle",
            remote_url=fixture.bundle_remote_url,
            project_root=tmp_path,
        )
        # Pre-seed an event + audit row to simulate prior triage.
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES (?,?,1,?,?,'{}','tr','2026-05-13T00:00:00Z')",
            ("prior-evt", "jira.assigned", "jira_assigned", "dedup-prior"),
        )
        await conn.commit()
        await insert_audit(
            conn,
            event_id="prior-evt",
            issue_key="SSWCI-100",
            comment_seq="1",
            status="posted",
            comment_id="prior-comment-1",
            posted_at=datetime(2026, 5, 13, 6, 30, tzinfo=UTC),
            created_at=datetime(2026, 5, 13, 6, 30, tzinfo=UTC),
        )
        await conn.commit()
        # Now fire a fresh non-force manual event.
        handler = _make_handler(
            db=conn,
            jira=jira,
            loki=FakeLokiClient(),
            ssh=FakeSshLogClient(),
            ssw_bundle=ssw,
        )
        event = _manual_event("SSWCI-100", force=False)
        await conn.execute(
            "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
            " payload_json, trace_id, created_at)"
            " VALUES (?,?,1,?,?,'{}','tr','2026-05-13T00:00:00Z')",
            (event.id, event.type, "jira_triage_manual", "dedup-new"),
        )
        await conn.commit()
        result = await handler.handle(
            event, _FakeCtx(claude_session_factory=FakeFactory(FakeClaudeSession()))
        )
        assert isinstance(result, Ack)
        # No new comment posted.
        assert jira.posted_comments() == []
        # Two audit rows: original 'posted' + new 'skipped_already_triaged'.
        latest = await find_latest(conn, "SSWCI-100")
        assert latest is not None
        assert latest.status == "skipped_already_triaged"
    finally:
        await conn.close()


# ── Quote-verification + redaction helpers (unit, no full pipeline) ──────────


def test_verify_evidence_quotes_passes_when_present() -> None:
    from daeyeon_bot.core.jira_triage.types import (
        EpicMeta,
        EvidenceItem,
        RunMeta,
        TicketRef,
        TimeWindow,
        TitleParse,
        TriageDraft,
    )

    snapshot = RunSnapshot(
        meta=RunMeta(
            ticket=TicketRef(project="SSWCI", issue_key="SSWCI-1", created_iso="x"),
            title=TitleParse(hostname="h", tc_name="TC-1-x"),
            epic=EpicMeta(epic_key="SSWCI-0", branch="b", commit="a" * 40),
            window=TimeWindow(
                start_ts=datetime(2026, 5, 13, tzinfo=UTC),
                end_ts=datetime(2026, 5, 13, 1, tzinfo=UTC),
                fallback=False,
            ),
            ssh=None,
            host_ip=None,
        ),
        error_log_excerpt="",
        test_code=None,
        product_code=(),
        loki_slices=(LokiSlice(stream="kernel", lines=("the quote here",), truncated=False),),
        ssh_artifacts=(),
        loki_error=None,
        ssh_error=None,
    )
    triage = TriageDraft(
        summary_md="x",
        domain="CpFw",
        severity="sev2",
        suspected_duplicates=(),
        needs_human=False,
        evidence=(EvidenceItem(source="loki.kernel", quote="the quote here", citation="t"),),
    )
    _verify_evidence_quotes(triage, snapshot)  # must not raise


def test_verify_evidence_quotes_rejects_fabricated() -> None:
    from daeyeon_bot.core.jira_triage.types import (
        EpicMeta,
        EvidenceItem,
        RunMeta,
        TicketRef,
        TimeWindow,
        TitleParse,
        TriageDraft,
    )

    snapshot = RunSnapshot(
        meta=RunMeta(
            ticket=TicketRef(project="SSWCI", issue_key="SSWCI-1", created_iso="x"),
            title=TitleParse(hostname="h", tc_name="TC-1-x"),
            epic=EpicMeta(epic_key="SSWCI-0", branch="b", commit="a" * 40),
            window=TimeWindow(
                start_ts=datetime(2026, 5, 13, tzinfo=UTC),
                end_ts=datetime(2026, 5, 13, 1, tzinfo=UTC),
                fallback=False,
            ),
            ssh=None,
            host_ip=None,
        ),
        error_log_excerpt="",
        test_code=None,
        product_code=(),
        loki_slices=(LokiSlice(stream="kernel", lines=("actual line",), truncated=False),),
        ssh_artifacts=(),
        loki_error=None,
        ssh_error=None,
    )
    triage = TriageDraft(
        summary_md="x",
        domain="CpFw",
        severity="sev2",
        suspected_duplicates=(),
        needs_human=False,
        evidence=(EvidenceItem(source="loki.kernel", quote="FABRICATED", citation="t"),),
    )
    with pytest.raises(PermanentError, match="fabricated evidence quote"):
        _verify_evidence_quotes(triage, snapshot)


def test_strip_code_fence_handles_json_wrapper() -> None:
    text = '```json\n{"a": 1}\n```'
    assert _strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_handles_plain_fence() -> None:
    text = '```\n{"a": 1}\n```'
    assert _strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_no_fence_passthrough() -> None:
    text = '{"a": 1}'
    assert _strip_code_fence(text) == '{"a": 1}'
