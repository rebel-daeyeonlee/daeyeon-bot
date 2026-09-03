"""End-to-end auto-trigger flow (T034).

Boots the daemon in-process with the polling trigger enabled, drives a
single observation through the full pipeline:

    trigger poll → events INSERT + state UPSERT (one TX) → dispatcher claims
    → handler posts → audit row `status='posted'`.

Then simulates a re-request (PR leaves the search set, then re-enters at
the same head SHA) and asserts a second posted review with the supersede
header (`Updated review for SHA …`).

The trigger uses `tests.fakes.gh_cli.FakeGh`, the handler uses
`FakeClaudeSession` — no network is touched.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from daeyeon_bot.app.config import load
from daeyeon_bot.app.container import ContainerOverrides
from daeyeon_bot.app.lifecycle import BootOptions, boot
from daeyeon_bot.infra import storage
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
                f"Reviewed at SHA {head_sha}. The change looks fine, with one "
                "nit on the second added line."
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
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""
[runtime]
state_dir = {str(state_dir)!r}

[logging]
level = "WARNING"
format = "console"

[github]
username = "daeyeon-lee"

[triggers.gh_review_requested]
enabled = true
poll_interval_seconds = 1

[handlers.echo]
enabled = false

[handlers.pr_review]
enabled = true
persona_skill = "pr-reviewer"
min_persona_chars = 50
# The poller fixture below is review-requested shaped, so pin the scope
# that searches for it — the production default is "self".
scope = "requested"

[routing]
"gh.review_requested" = ["pr_review"]
"pr.review.manual" = ["pr_review"]
""".lstrip(),
        encoding="utf-8",
    )
    return cfg


async def _wait_for(
    predicate: Callable[[], Awaitable[bool]], *, attempts: int = 200, delay: float = 0.05
) -> None:
    for _ in range(attempts):
        if await predicate():
            return
        await asyncio.sleep(delay)
    raise AssertionError("predicate never satisfied")


async def test_auto_trigger_posts_review_then_supersedes_on_re_request(
    config_file: Path,
    state_dir: Path,
    skills_root: Path,
) -> None:
    head_sha = "abc1234567890def1234"
    repo = "octo/cat"
    pr_number = 7

    fake_gh = FakeGh(user_login="daeyeon-lee")
    fake_gh.add_pr(
        repo,
        pr_number,
        head_sha=head_sha,
        author="alice",
        requested=("daeyeon-lee",),
        files=[{"filename": "lib/foo.py", "patch": _PATCH_HUNK, "changes": 4}],
        in_search_set=True,
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

    try:
        # First poll lands an event + posts review #1.
        async def _one_review_posted() -> bool:
            return len(fake_gh.posted_reviews()) >= 1

        await _wait_for(_one_review_posted, attempts=400, delay=0.05)

        first = fake_gh.posted_reviews()[0]
        assert first["commit_id"] == head_sha
        assert head_sha in first["body"]
        assert "Updated review for SHA" not in first["body"]

        # Simulate re-request at the same head SHA. The trigger polls every
        # 1 s, so wait for the *withdrawal* poll (CASE 5: in_pending_set→0)
        # before re-adding so the next poll fires CASE 2 (gen bumped, emit).
        fake_gh.remove_from_search(repo, pr_number)

        async def _state_pending_cleared() -> bool:
            async with storage.connection(db_path) as conn:
                async with conn.execute(
                    "SELECT in_pending_set FROM gh_review_requested_state"
                    " WHERE repo = ? AND pr_number = ?",
                    (repo, pr_number),
                ) as cur:
                    row = await cur.fetchone()
            return row is not None and bool(row["in_pending_set"]) is False

        await _wait_for(_state_pending_cleared, attempts=400, delay=0.05)
        fake_gh.add_to_search(repo, pr_number)

        async def _two_reviews_posted() -> bool:
            return len(fake_gh.posted_reviews()) >= 2

        await _wait_for(_two_reviews_posted, attempts=600, delay=0.05)

        second = fake_gh.posted_reviews()[1]
        assert second["commit_id"] == head_sha
        assert "Updated review for SHA" in second["body"]
    finally:
        stop.set()
        await asyncio.wait_for(boot_task, timeout=10.0)

    # `record_supersede` UPDATEs the original audit row in place: the prior
    # review_id is appended to `superseded_review_ids` (JSON), and the row's
    # `review_id` is replaced with the new one. So we expect ONE row with
    # status='posted' and a non-empty supersede history.
    async with storage.connection(db_path) as conn:
        async with conn.execute(
            "SELECT status, review_id, superseded_review_ids FROM pr_review_audit ORDER BY id"
        ) as cur:
            audits = await cur.fetchall()
        async with conn.execute(
            "SELECT request_gen, in_pending_set FROM gh_review_requested_state"
            " WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ) as cur:
            state_row = await cur.fetchone()

    posted_audits = [a for a in audits if a["status"] == "posted"]
    assert len(posted_audits) == 1
    final_audit = posted_audits[0]
    superseded = json.loads(final_audit["superseded_review_ids"] or "[]")
    assert len(superseded) == 1, (
        f"expected one prior review in supersede history, got {superseded!r}"
    )
    assert final_audit["review_id"] == fake_gh.posted_reviews()[1]["review_id"]
    assert superseded[0] == fake_gh.posted_reviews()[0]["review_id"]
    assert state_row is not None
    assert state_row["request_gen"] == 2
    assert bool(state_row["in_pending_set"]) is True
