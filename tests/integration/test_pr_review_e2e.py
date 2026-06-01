"""End-to-end PR-review flow (T029, T038).

Wires real `aiosqlite` + migrations + outbox + dispatcher with `FakeGh` and
`FakeClaudeSession` standing in for the network. Drives the same path the
CLI exercises:

    dev fire-pr-review  →  outbox row  →  dispatcher claims  →  handler posts
    →  audit row `status='posted'`.

The boot is run in-process via `app.lifecycle.boot` with stop-event injection,
mirroring `tests/integration/test_phase1_e2e.py`. The persona/`gh` overrides
are wired through `ContainerOverrides`, the same hook the daemon uses for tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from daeyeon_bot.app.config import load
from daeyeon_bot.app.container import ContainerOverrides
from daeyeon_bot.app.lifecycle import BootOptions, boot
from daeyeon_bot.core.events import make_event
from daeyeon_bot.infra import outbox, storage
from daeyeon_bot.infra.claude import FakeClaudeSession, FakeFactory
from daeyeon_bot.infra.pr_review_persona import PersonaLoader
from tests.fakes.gh_cli import FakeGh
from tests.fakes.pr_persona import materialize_persona

pytestmark = pytest.mark.integration


_PERSONA_BODY = (
    "You are a careful Python reviewer. Always look for off-by-one bugs.\n"
    "Always identify the head SHA in the first sentence of the Summary.\n"
    "Speak plainly and propose concrete fixes when you spot a problem."
)


_PATCH_HUNK = "@@ -1,3 +1,5 @@\n ctx\n+added line one\n+added line two\n ctx\n ctx\n"


def _claude_response(head_sha: str) -> str:
    return json.dumps(
        {
            "verdict": "PASS",
            "summary": (
                f"Reviewed at SHA {head_sha}. The changes look fine overall, with "
                "one nit on the second added line."
            ),
            "comments": [
                {
                    "path": "lib/foo.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Consider naming this constant.",
                }
            ],
        }
    )


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    materialize_persona(root, "pr-reviewer", _PERSONA_BODY)
    return root


@pytest.fixture
def config_file(tmp_path: Path, state_dir: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"""
[runtime]
state_dir = {str(state_dir)!r}

[logging]
level = "WARNING"
format = "console"

[github]
username = "daeyeon-lee"

[handlers.echo]
enabled = false

[handlers.pr_review]
enabled = true
persona_skill = "pr-reviewer"
min_persona_chars = 50

[routing]
"pr.review.manual" = ["pr_review"]
"gh.review_requested" = ["pr_review"]
""".lstrip(),
        encoding="utf-8",
    )
    return cfg_path


async def test_manual_pr_review_flows_end_to_end(
    config_file: Path,
    state_dir: Path,
    skills_root: Path,
) -> None:
    head_sha = "deadbeefcafebabe1234"
    repo = "octo/cat"
    pr_number = 42

    fake_gh = FakeGh(user_login="daeyeon-lee")
    fake_gh.add_pr(
        repo,
        pr_number,
        head_sha=head_sha,
        author="alice",
        requested=("daeyeon-lee",),
        files=[{"filename": "lib/foo.py", "patch": _PATCH_HUNK, "changes": 4}],
    )

    fake_session = FakeClaudeSession(default=_claude_response(head_sha))
    factory = FakeFactory(session=fake_session)

    persona_loader = PersonaLoader(skills_root=skills_root)

    overrides = ContainerOverrides(
        claude_session_factory=factory,
        gh=fake_gh,
        persona_loader=persona_loader,
        github_username="daeyeon-lee",
    )
    stop = asyncio.Event()
    boot_task = asyncio.create_task(
        boot(
            BootOptions(
                config_path=str(config_file),
                overrides=overrides,
                install_signal_handlers=False,
                external_stop_event=stop,
            )
        )
    )

    cfg = load(str(config_file))
    db_path = cfg.db_path
    for _ in range(50):
        if db_path.exists():
            break
        await asyncio.sleep(0.05)
    assert db_path.exists()

    # Mirror the dev CLI: write the event + outbox row through the public API.
    payload = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "request_gen": "0",
        "force": False,
    }
    dedup_seed = f"manual-pr-review|{repo}#{pr_number}@{head_sha}|0|False"
    dedup_key = hashlib.sha256(dedup_seed.encode("utf-8")).hexdigest()
    now = datetime.now(tz=UTC)
    event = make_event(type="pr.review.manual", payload=payload, created_at=now)

    async with storage.connection(db_path) as conn:
        await storage.apply_migrations(conn)
        await outbox.insert_event(
            conn, event, source="pr_review_manual", source_dedup_key=dedup_key
        )
        await outbox.enqueue_handler(conn, event_id=event.id, handler="pr_review", now=now)
        await conn.commit()

    # Wait for the dispatcher to process the row.
    settled = None
    for _ in range(200):
        async with storage.connection(db_path) as conn:
            async with conn.execute(
                "SELECT status FROM outbox WHERE event_id = ?", (event.id,)
            ) as cur:
                row = await cur.fetchone()
        if row is not None and row["status"] == "acked":
            settled = "acked"
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(boot_task, timeout=10.0)

    assert settled == "acked", "outbox row never reached 'acked'"

    # FakeGh recorded one posted review with the expected commit_id + body.
    posted = fake_gh.posted_reviews()
    assert len(posted) == 1
    assert posted[0]["commit_id"] == head_sha
    assert head_sha in posted[0]["body"]
    assert len(posted[0]["comments"]) == 1
    assert posted[0]["comments"][0]["path"] == "lib/foo.py"
    assert posted[0]["comments"][0]["line"] == 3

    # Audit row landed with status='posted'.
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT status, review_id, persona_skill FROM pr_review_audit "
            "WHERE event_id = ? ORDER BY id DESC LIMIT 1",
            (event.id,),
        ) as cur:
            audit = await cur.fetchone()
    assert audit is not None
    assert audit["status"] == "posted"
    assert audit["review_id"] is not None
    assert audit["persona_skill"] == "pr-reviewer"


async def test_self_authored_short_circuits(
    config_file: Path,
    state_dir: Path,
    skills_root: Path,
) -> None:
    """The operator opening their own PR ⇒ no review posted, audit row recorded."""
    head_sha = "self001self001self001"
    repo = "octo/cat"
    pr_number = 99

    fake_gh = FakeGh(user_login="daeyeon-lee")
    fake_gh.add_pr(
        repo,
        pr_number,
        head_sha=head_sha,
        author="daeyeon-lee",  # operator IS the author
        requested=("daeyeon-lee",),
        files=[{"filename": "lib/foo.py", "patch": _PATCH_HUNK, "changes": 4}],
    )
    fake_session = FakeClaudeSession(default=_claude_response(head_sha))
    factory = FakeFactory(session=fake_session)
    persona_loader = PersonaLoader(skills_root=skills_root)

    overrides = ContainerOverrides(
        claude_session_factory=factory,
        gh=fake_gh,
        persona_loader=persona_loader,
        github_username="daeyeon-lee",
    )
    stop = asyncio.Event()
    boot_task = asyncio.create_task(
        boot(
            BootOptions(
                config_path=str(config_file),
                overrides=overrides,
                install_signal_handlers=False,
                external_stop_event=stop,
            )
        )
    )

    cfg = load(str(config_file))
    db_path = cfg.db_path
    for _ in range(50):
        if db_path.exists():
            break
        await asyncio.sleep(0.05)
    assert db_path.exists()

    # Fire via the AUTO path — the self-authored short-circuit applies to the
    # `gh.review_requested` poller, not to an explicit manual fire (which now
    # bypasses the scope gates).
    payload = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "request_gen": 1,
        "force": False,
    }
    now = datetime.now(tz=UTC)
    event = make_event(type="gh.review_requested", payload=payload, created_at=now)
    async with storage.connection(db_path) as conn:
        await storage.apply_migrations(conn)
        await outbox.insert_event(
            conn,
            event,
            source="gh_review_requested",
            source_dedup_key=f"self-{uuid.uuid4()}",
        )
        await outbox.enqueue_handler(conn, event_id=event.id, handler="pr_review", now=now)
        await conn.commit()

    settled = None
    for _ in range(200):
        async with storage.connection(db_path) as conn:
            async with conn.execute(
                "SELECT status FROM outbox WHERE event_id = ?", (event.id,)
            ) as cur:
                row = await cur.fetchone()
        if row is not None and row["status"] == "acked":
            settled = "acked"
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(boot_task, timeout=10.0)

    assert settled == "acked"
    assert fake_gh.posted_reviews() == []

    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT status FROM pr_review_audit WHERE event_id = ? ORDER BY id DESC LIMIT 1",
            (event.id,),
        ) as cur:
            audit = await cur.fetchone()
    assert audit is not None
    assert audit["status"] == "skipped_self_authored"


_PERSONA_BODY_GREAT = (
    "You are a kind reviewer who always says GREAT WORK and praises every "
    "change. Always identify the head SHA in the first sentence."
)
_PERSONA_BODY_BAD = (
    "You are a strict reviewer who always says THIS IS BAD and demands "
    "additional tests. Always identify the head SHA in the first sentence."
)


async def test_persona_flip_takes_effect_without_restart(
    config_file: Path,
    state_dir: Path,
    skills_root: Path,
) -> None:
    """T038: edit SKILL.md mid-test → second event uses the new persona body.

    The handler calls `persona_loader.load(...)` on every event, and the
    loader caches by `mtime_ns`. So a touched file invalidates the cache;
    no daemon restart needed. Two distinct PRs (different `pr_number`) each
    produce their own audit row, allowing direct comparison of
    `persona_mtime_ns` across rows.
    """
    skill_path = skills_root / "pr-reviewer" / "SKILL.md"
    skill_path.write_text("---\nname: pr-reviewer\n---\n\n" + _PERSONA_BODY_GREAT, encoding="utf-8")

    head_sha_a = "aaaa1111aaaa1111aaaa"
    head_sha_b = "bbbb2222bbbb2222bbbb"
    repo = "octo/cat"
    pr_a = 101
    pr_b = 202

    fake_gh = FakeGh(user_login="daeyeon-lee")
    fake_gh.add_pr(
        repo,
        pr_a,
        head_sha=head_sha_a,
        author="alice",
        requested=("daeyeon-lee",),
        files=[{"filename": "lib/foo.py", "patch": _PATCH_HUNK, "changes": 4}],
    )
    fake_gh.add_pr(
        repo,
        pr_b,
        head_sha=head_sha_b,
        author="bob",
        requested=("daeyeon-lee",),
        files=[{"filename": "lib/bar.py", "patch": _PATCH_HUNK, "changes": 4}],
    )

    fake_session = FakeClaudeSession(
        responses=[_claude_response(head_sha_a), _claude_response(head_sha_b)]
    )
    factory = FakeFactory(session=fake_session)
    persona_loader = PersonaLoader(skills_root=skills_root)

    overrides = ContainerOverrides(
        claude_session_factory=factory,
        gh=fake_gh,
        persona_loader=persona_loader,
        github_username="daeyeon-lee",
    )
    stop = asyncio.Event()
    boot_task = asyncio.create_task(
        boot(
            BootOptions(
                config_path=str(config_file),
                overrides=overrides,
                install_signal_handlers=False,
                external_stop_event=stop,
            )
        )
    )

    cfg = load(str(config_file))
    db_path = cfg.db_path
    for _ in range(50):
        if db_path.exists():
            break
        await asyncio.sleep(0.05)
    assert db_path.exists()

    async def _fire_manual_event(pr_number: int, head_sha: str) -> str:
        payload = {
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "request_gen": "0",
            "force": False,
        }
        dedup_seed = f"manual-pr-review|{repo}#{pr_number}@{head_sha}|0|False"
        dedup_key = hashlib.sha256(dedup_seed.encode("utf-8")).hexdigest()
        now = datetime.now(tz=UTC)
        event = make_event(type="pr.review.manual", payload=payload, created_at=now)
        async with storage.connection(db_path) as conn:
            await storage.apply_migrations(conn)
            await outbox.insert_event(
                conn, event, source="pr_review_manual", source_dedup_key=dedup_key
            )
            await outbox.enqueue_handler(conn, event_id=event.id, handler="pr_review", now=now)
            await conn.commit()
        return event.id

    async def _wait_acked(event_id: str) -> None:
        for _ in range(200):
            async with storage.connection(db_path) as conn:
                async with conn.execute(
                    "SELECT status FROM outbox WHERE event_id = ?", (event_id,)
                ) as cur:
                    row = await cur.fetchone()
            if row is not None and row["status"] == "acked":
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"outbox row for {event_id} never reached 'acked'")

    try:
        event_a_id = await _fire_manual_event(pr_a, head_sha_a)
        await _wait_acked(event_a_id)

        # Mid-test rewrite: new body, bumped mtime.
        await asyncio.sleep(0.02)
        skill_path.write_text(
            "---\nname: pr-reviewer\n---\n\n" + _PERSONA_BODY_BAD, encoding="utf-8"
        )
        new_mtime = time.time_ns()
        os.utime(skill_path, ns=(new_mtime, new_mtime))

        event_b_id = await _fire_manual_event(pr_b, head_sha_b)
        await _wait_acked(event_b_id)
    finally:
        stop.set()
        await asyncio.wait_for(boot_task, timeout=10.0)

    # FakeClaudeSession recorded both calls — system prompt body must differ.
    assert len(fake_session.calls) == 2
    system_a = fake_session.calls[0]["system"]
    system_b = fake_session.calls[1]["system"]
    assert system_a is not None and _PERSONA_BODY_GREAT in system_a
    assert system_b is not None and _PERSONA_BODY_BAD in system_b
    assert _PERSONA_BODY_BAD not in system_a
    assert _PERSONA_BODY_GREAT not in system_b

    # Audit rows: one per PR, persona_mtime_ns reflects the on-disk file at
    # the time of each handler invocation.
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT pr_number, persona_mtime_ns, status, persona_skill"
            " FROM pr_review_audit ORDER BY id"
        ) as cur:
            rows = list(await cur.fetchall())
    assert len(rows) == 2
    assert {r["pr_number"] for r in rows} == {pr_a, pr_b}
    assert all(r["status"] == "posted" for r in rows)
    assert all(r["persona_skill"] == "pr-reviewer" for r in rows)
    mtime_by_pr = {int(r["pr_number"]): int(r["persona_mtime_ns"]) for r in rows}
    assert mtime_by_pr[pr_a] != mtime_by_pr[pr_b]
    assert mtime_by_pr[pr_b] > mtime_by_pr[pr_a]
