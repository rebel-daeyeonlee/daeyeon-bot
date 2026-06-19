"""ci_triage end-to-end (feature 003 P1).

Wires the REAL container (`container.build`) + real aiosqlite + migrations + a
REAL git OnCall-wiki fixture (file:// remote) + fakes for Slack / gh / Claude,
then runs the handler the registry instantiated. Validates that:
  fire ci.triage.manual → container wires CiTriageDeps → handler clones the wiki,
  collects the (fake) failed log, calls (fake) Claude, posts to the dry_run
  channel, and writes a `posted` audit row.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from daeyeon_bot.app import container as container_mod
from daeyeon_bot.app.config import Config
from daeyeon_bot.app.container import ContainerOverrides
from daeyeon_bot.core.events import Event, make_event
from daeyeon_bot.core.results import Ack
from daeyeon_bot.infra.ci_triage_audit import find_latest_for_message
from daeyeon_bot.infra.claude import FakeClaudeSession, FakeFactory
from daeyeon_bot.infra.oncall_wiki import INCIDENTS_DIR, RECOVERY_PLAYBOOK_PATH, OncallWiki
from daeyeon_bot.infra.slack import PostResult
from daeyeon_bot.infra.storage import apply_migrations, open_db

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 19, 7, 0, 0, tzinfo=UTC)

_GOOD_TRIAGE = json.dumps(
    {
        "attribution": "infra_env",
        "classification": "environment",
        "owner_area": "DevOps",
        "confidence": "medium",
        "summary": "QEMU golden base 이미지 소실",
        "log_evidence": [
            {"quote": "rsync ... golden-base failed: No such file", "citation": "premerge / result"}
        ],
        "wiki_matches": [
            {"path": "wiki/oncall/incidents/qemu-golden-base-image-missing.md", "why": "sig"}
        ],
        "likely_cause": "golden base image deleted from NFS",
        "known_remedy": "rebuild golden base image",
        "recommended_action": "golden image 재빌드 후 rerun",
        "rerun_advice": "needs_investigation",
        "needs_human": True,
    }
)


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
        self.posts.append({"channel": channel_id, "text": text, "thread_ts": thread_ts})
        return PostResult(channel=channel_id, ts="200.5")


@dataclass(slots=True)
class _FakeGh:
    async def run_failed_job_logs(self, repo: str, run_id: str) -> str:
        return (
            "premerge / result | rsync ... golden-base failed: No such file\n"
            "##[error]Process completed with exit code 1."
        )

    async def run_failed_annotations(self, repo: str, run_id: str) -> str:
        return ""

    async def failed_jobs(self, repo: str, run_id: str) -> list[Any]:
        return []


@dataclass(slots=True)
class _FakeCtx:
    claude_session_factory: Any
    trace_id: str = "trace-e2e"
    clock: Any = None

    def __post_init__(self) -> None:
        if self.clock is None:

            class _Clk:
                def now(self) -> datetime:
                    return _NOW

            self.clock = _Clk()


def _git(args: list[str], cwd: Path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _seed_remote_vault(src: Path) -> None:
    inc = src / INCIDENTS_DIR
    inc.mkdir(parents=True)
    (src / RECOVERY_PLAYBOOK_PATH).parent.mkdir(parents=True, exist_ok=True)
    (src / RECOVERY_PLAYBOOK_PATH).write_text("# Recovery Playbook\n", encoding="utf-8")
    (inc / "qemu-golden-base-image-missing.md").write_text(
        '---\nsignature: "VM creation failed (golden base image missing)"\ndomain: DevOps\n'
        "---\n# golden base\nrsync golden-base failed\n",
        encoding="utf-8",
    )
    _git(["init", "-q"], src)
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "init"], src)


def _config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "runtime": {"state_dir": str(tmp_path)},
            "handlers": {
                "ci_triage": {
                    "enabled": True,
                    "dry_run_channel": "C_DRY",
                    "persona_skill": "daeyeon-bot-ci-triage",
                }
            },
            "routing": {"ci.triage.manual": ["ci_triage"]},
        }
    )


async def _seed_event(conn: aiosqlite.Connection, event: Event) -> None:
    await conn.execute(
        "INSERT INTO events(id, type, schema_version, source, source_dedup_key,"
        " payload_json, trace_id, created_at)"
        " VALUES (?, ?, 1, 'ci_triage_manual', ?, '{}', 'tr', '2026-06-19T00:00:00Z')",
        (event.id, event.type, f"d-{event.id}"),
    )
    await conn.commit()


async def test_ci_triage_manual_fire_e2e(tmp_path: Path) -> None:
    # Real git OnCall-wiki fixture (file:// remote).
    src = tmp_path / "wiki-remote"
    src.mkdir()
    _seed_remote_vault(src)

    db = await open_db(tmp_path / "state.db")
    try:
        await apply_migrations(db)

        slack = _FakeSlack()
        wiki = OncallWiki(
            clone_path=tmp_path / "var" / "ssw-devops-oncall",
            known_hosts_path=tmp_path / "kh",
            remote_url=f"file://{src}",
            project_root=tmp_path,
        )
        overrides = ContainerOverrides(
            claude_session_factory=FakeFactory(session=FakeClaudeSession(responses=[_GOOD_TRIAGE])),
            slack=slack,
            oncall_wiki=wiki,
            gh=_FakeGh(),
            project_root=tmp_path,
        )
        container = await container_mod.build(_config(tmp_path), db, overrides=overrides)

        # The registry instantiated ci_triage and routes the manual event to it.
        records = container.handlers.handlers_for("ci.triage.manual")
        assert [r.name for r in records] == ["ci_triage"]
        handler = records[0].instance

        event = make_event(
            type="ci.triage.manual",
            payload={"repo": "rebellions-sw/ssw-bundle", "run_id": "27758520154", "force": False},
            created_at=_NOW,
        )
        await _seed_event(db, event)

        result = await handler.handle(event, _FakeCtx(container.claude_session_factory))  # type: ignore[attr-defined]
        assert isinstance(result, Ack)

        # Posted to the dry_run channel.
        assert len(slack.posts) == 1
        assert slack.posts[0]["channel"] == "C_DRY"
        assert "infra_env" in slack.posts[0]["text"]

        # The real wiki was cloned (file:// remote) and the audit row landed.
        assert (tmp_path / "var" / "ssw-devops-oncall" / RECOVERY_PLAYBOOK_PATH).is_file()
        audit = await find_latest_for_message(
            db, channel_id="manual:rebellions-sw/ssw-bundle", message_ts="27758520154"
        )
        assert audit is not None
        assert audit.status == "posted"
        assert audit.attribution == "infra_env"
        assert audit.persona_skill == "daeyeon-bot-ci-triage"
        # The wiki match (from the real cloned fixture) made it into the audit.
        assert any("qemu-golden-base" in p for p in audit.wiki_matches)
    finally:
        await db.close()
