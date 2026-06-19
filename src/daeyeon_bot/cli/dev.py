"""Dev commands: fire / call / repl. Not for production use."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import typer

from daeyeon_bot.app.config import load
from daeyeon_bot.app.registry import build_handler_registry
from daeyeon_bot.cli._dev_payloads import (
    build_ci_triage_payload,
    build_jira_triage_payload,
    build_pr_review_payload,
)
from daeyeon_bot.core.errors import AuthError, PermanentError
from daeyeon_bot.core.events import Event, make_event
from daeyeon_bot.core.results import Ack, DeadLetter, HandlerResult, Retry
from daeyeon_bot.core.time import Clock, SystemClock
from daeyeon_bot.infra import outbox, storage
from daeyeon_bot.infra.claude import FakeClaudeSession
from daeyeon_bot.infra.gh_cli import GhCli

app = typer.Typer(
    help="Developer utilities: fire triggers, call handlers, IPython repl.", no_args_is_help=True
)


_PR_REF_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<n>\d+)/?$"
)
_PR_REF_SHORT_RE = re.compile(r"^(?P<owner>[^/]+)/(?P<repo>[^/#]+)#(?P<n>\d+)$")


def _parse_pr_ref(ref: str) -> tuple[str, int]:
    """Parse `owner/repo#N` or `https://github.com/owner/repo/pull/N`."""
    m = _PR_REF_URL_RE.match(ref) or _PR_REF_SHORT_RE.match(ref)
    if m is None:
        raise typer.BadParameter(
            "PR ref must be 'owner/repo#N' or 'https://github.com/owner/repo/pull/N'"
        )
    return f"{m['owner']}/{m['repo']}", int(m["n"])


@app.command(
    "fire",
    help="Fire a trigger to emit an event (writes through the normal outbox path).",
)
def fire(
    trigger: str,
    message: str = typer.Option("", "--message", "-m", help="Payload message field."),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    if trigger != "manual":
        raise typer.BadParameter(
            f"only 'manual' is supported by `dev fire`; got {trigger!r}."
            " For PR review use `dev fire-pr-review`; `gh_review_requested` polls itself."
        )
    asyncio.run(_fire_manual(message=message, config_path=config))


@app.command(
    "fire-pr-review",
    help="Enqueue a manual PR-review event. Equivalent to a re-request from the operator.",
)
def fire_pr_review(
    pr: str = typer.Option(..., "--pr", help="PR ref: 'owner/repo#N' or full GitHub URL."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-review at same SHA; appends supersede header."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the event JSON instead of writing it."
    ),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    asyncio.run(_fire_pr_review(pr=pr, force=force, dry_run=dry_run, config_path=config))


async def _fire_manual(*, message: str, config_path: str | None) -> None:
    cfg = load(config_path)
    cfg.state_dir_path.mkdir(parents=True, exist_ok=True)
    routing = cfg.routing.get("manual.message", [])
    if not routing:
        raise typer.BadParameter(
            "no handlers configured for 'manual.message'. Edit config.toml's [routing] section."
        )

    now = datetime.now(tz=UTC)
    event = make_event(type="manual.message", payload={"message": message}, created_at=now)
    dedup_key = f"cli-{uuid.uuid4()}"

    async with storage.connection(cfg.db_path) as conn:
        await storage.apply_migrations(conn)
        ok = await outbox.insert_event(conn, event, source="manual", source_dedup_key=dedup_key)
        if not ok:
            raise typer.Exit(code=1)
        for handler in routing:
            await outbox.enqueue_handler(conn, event_id=event.id, handler=handler, now=now)
        await conn.commit()
    typer.echo(event.id)


@dataclass(slots=True)
class _DevHandlerCtx:
    """Concrete HandlerContext for ad-hoc dev invocations."""

    clock: Clock
    trace_id: str
    claude_session_factory: object


@app.command(
    "call",
    help="Invoke a handler directly with an event JSON, BYPASSING the outbox. Dev only.",
)
def call(
    handler: str,
    event_json: str = typer.Option(
        "",
        "--event-json",
        help="Full event JSON: {type, payload, ...}. Mutually exclusive with --message.",
    ),
    message: str = typer.Option(
        "",
        "--message",
        "-m",
        help="Shortcut: build a manual.message event with this payload.message.",
    ),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    if bool(event_json) == bool(message):
        raise typer.BadParameter("specify exactly one of --event-json or --message")
    result = asyncio.run(
        _call_handler(
            handler_name=handler, event_json=event_json, message=message, config_path=config
        )
    )
    typer.echo(_render_result(result))


async def _call_handler(
    *, handler_name: str, event_json: str, message: str, config_path: str | None
) -> HandlerResult:
    cfg = load(config_path)
    registry = build_handler_registry(cfg)
    record = registry.by_name.get(handler_name)
    if record is None:
        raise typer.BadParameter(f"unknown or disabled handler: {handler_name!r}")

    event = _build_event(event_json=event_json, message=message)
    ctx = _DevHandlerCtx(
        clock=SystemClock(),
        trace_id=event.trace_id,
        claude_session_factory=lambda: FakeClaudeSession(default="dev-call"),
    )
    return await record.instance.handle(event, ctx)  # type: ignore[attr-defined,no-any-return]


def _build_event(*, event_json: str, message: str) -> Event:
    now = datetime.now(tz=UTC)
    if message:
        return make_event(type="manual.message", payload={"message": message}, created_at=now)
    try:
        raw: object = json.loads(event_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--event-json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise typer.BadParameter("--event-json must be a JSON object")
    data = cast("dict[str, object]", raw)
    event_type = data.get("type")
    if not isinstance(event_type, str):
        raise typer.BadParameter("--event-json must include a string 'type' field")
    payload_raw = data.get("payload", {})
    if not isinstance(payload_raw, dict):
        raise typer.BadParameter("--event-json 'payload' must be an object")
    payload = cast("dict[str, object]", payload_raw)
    return make_event(type=event_type, payload=payload, created_at=now)


def _render_result(result: HandlerResult) -> str:
    match result:
        case Ack():
            return "Ack"
        case Retry(after_s=after):
            return f"Retry(after_s={after})"
        case DeadLetter(reason=reason):
            return f"DeadLetter(reason={reason!r})"


async def _fire_pr_review(*, pr: str, force: bool, dry_run: bool, config_path: str | None) -> None:
    """Build a `pr.review.manual` event and enqueue it via the outbox."""
    repo, pr_number = _parse_pr_ref(pr)
    cfg = load(config_path)
    cfg.state_dir_path.mkdir(parents=True, exist_ok=True)

    routing = cfg.routing.get("pr.review.manual", [])
    if not routing:
        raise typer.BadParameter(
            "no handlers configured for 'pr.review.manual'. Edit config.toml's [routing] section."
        )

    gh = GhCli(timeout_seconds=cfg.github.gh_call_timeout_seconds)
    try:
        pr_payload = await gh.pr_get(repo, pr_number)
    except (AuthError, PermanentError) as exc:
        typer.echo(f"failed to fetch PR: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    head_obj = pr_payload.get("head")
    head_sha = ""
    if isinstance(head_obj, dict):
        head_block = cast("dict[str, object]", head_obj)
        sha = head_block.get("sha")
        if isinstance(sha, str):
            head_sha = sha
    if not head_sha:
        typer.echo(f"PR {pr} has no head SHA; aborting", err=True)
        raise typer.Exit(code=1)

    payload, dedup_key = build_pr_review_payload(
        repo=repo, pr_number=pr_number, head_sha=head_sha, force=force
    )

    now = datetime.now(tz=UTC)
    event = make_event(type="pr.review.manual", payload=payload, created_at=now)

    if dry_run:
        typer.echo(
            json.dumps(
                {
                    "event_id": event.id,
                    "type": event.type,
                    "payload": payload,
                    "source_dedup_key": dedup_key,
                    "routes_to": routing,
                },
                indent=2,
            )
        )
        return

    async with storage.connection(cfg.db_path) as conn:
        await storage.apply_migrations(conn)
        ok = await outbox.insert_event(
            conn, event, source="pr_review_manual", source_dedup_key=dedup_key
        )
        if not ok:
            typer.echo("duplicate dedup key — an identical event is already queued", err=True)
            raise typer.Exit(code=1)
        for handler in routing:
            await outbox.enqueue_handler(conn, event_id=event.id, handler=handler, now=now)
        await conn.commit()
    typer.echo(event.id)


_JIRA_ISSUE_RE = re.compile(r"^(?P<key>[A-Z]+-\d+)$")
_JIRA_URL_RE = re.compile(r"^https?://[^/]+/browse/(?P<key>[A-Z]+-\d+)/?$")


def _parse_issue_key(ref: str) -> str:
    """Parse `SSWCI-NNNN` or `https://<jira>/browse/SSWCI-NNNN`."""
    m = _JIRA_ISSUE_RE.match(ref) or _JIRA_URL_RE.match(ref)
    if m is None:
        raise typer.BadParameter(
            "issue ref must be 'PROJECT-N' (e.g. 'SSWCI-16787') or a /browse/ URL"
        )
    return m["key"]


@app.command(
    "fire-jira-triage",
    help="Enqueue a manual jira_triage event. Same audit-row dedup as the auto path.",
)
def fire_jira_triage(
    issue: str = typer.Option(
        ..., "--issue", help="Issue key (e.g. 'SSWCI-16787') or /browse/ URL."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Re-triage even when an audit row already exists; appends supersede header.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the event JSON instead of writing it."
    ),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    asyncio.run(_fire_jira_triage(issue=issue, force=force, dry_run=dry_run, config_path=config))


async def _fire_jira_triage(
    *, issue: str, force: bool, dry_run: bool, config_path: str | None
) -> None:
    """Build a `jira.triage.manual` event and enqueue it via the outbox."""
    issue_key = _parse_issue_key(issue)
    cfg = load(config_path)
    cfg.state_dir_path.mkdir(parents=True, exist_ok=True)

    routing = cfg.routing.get("jira.triage.manual", [])
    if not routing:
        raise typer.BadParameter(
            "no handlers configured for 'jira.triage.manual'. Edit config.toml's [routing] section."
        )

    payload, dedup_key = build_jira_triage_payload(issue_key=issue_key, force=force)

    now = datetime.now(tz=UTC)
    event = make_event(type="jira.triage.manual", payload=payload, created_at=now)

    if dry_run:
        typer.echo(
            json.dumps(
                {
                    "event_id": event.id,
                    "type": event.type,
                    "payload": payload,
                    "source_dedup_key": dedup_key,
                    "routes_to": routing,
                },
                indent=2,
            )
        )
        return

    async with storage.connection(cfg.db_path) as conn:
        await storage.apply_migrations(conn)
        ok = await outbox.insert_event(
            conn, event, source="jira_triage_manual", source_dedup_key=dedup_key
        )
        if not ok:
            typer.echo("duplicate dedup key — an identical event is already queued", err=True)
            raise typer.Exit(code=1)
        for handler in routing:
            await outbox.enqueue_handler(conn, event_id=event.id, handler=handler, now=now)
        await conn.commit()
    typer.echo(event.id)


@app.command(
    "fire-ci-triage",
    help="Enqueue a manual ci_triage event for a GitHub Actions run (feature 003).",
)
def fire_ci_triage(
    repo: str = typer.Option(..., "--repo", help="owner/repo (e.g. 'rebellions-sw/ssw-bundle')."),
    run: str = typer.Option(..., "--run", help="GitHub Actions run id (numeric)."),
    channel: str | None = typer.Option(
        None, "--channel", help="Source alert channel id; with --message-ts, reply in that thread."
    ),
    message_ts: str | None = typer.Option(
        None, "--message-ts", help="Source alert message ts; with --channel, reply in that thread."
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-triage even when a posted audit row exists."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the event JSON instead of writing it."
    ),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    asyncio.run(
        _fire_ci_triage(
            repo=repo,
            run=run,
            channel=channel,
            message_ts=message_ts,
            force=force,
            dry_run=dry_run,
            config_path=config,
        )
    )


async def _fire_ci_triage(
    *,
    repo: str,
    run: str,
    channel: str | None,
    message_ts: str | None,
    force: bool,
    dry_run: bool,
    config_path: str | None,
) -> None:
    """Build a `ci.triage.manual` event and enqueue it via the outbox."""
    cfg = load(config_path)
    cfg.state_dir_path.mkdir(parents=True, exist_ok=True)

    routing = cfg.routing.get("ci.triage.manual", [])
    if not routing:
        raise typer.BadParameter(
            "no handlers configured for 'ci.triage.manual'. Edit config.toml's [routing] section."
        )

    payload, dedup_key = build_ci_triage_payload(
        repo=repo, run_id=run, force=force, channel_id=channel, message_ts=message_ts
    )
    now = datetime.now(tz=UTC)
    event = make_event(type="ci.triage.manual", payload=payload, created_at=now)

    if dry_run:
        typer.echo(
            json.dumps(
                {
                    "event_id": event.id,
                    "type": event.type,
                    "payload": payload,
                    "source_dedup_key": dedup_key,
                    "routes_to": routing,
                },
                indent=2,
            )
        )
        return

    async with storage.connection(cfg.db_path) as conn:
        await storage.apply_migrations(conn)
        ok = await outbox.insert_event(
            conn, event, source="ci_triage_manual", source_dedup_key=dedup_key
        )
        if not ok:
            typer.echo("duplicate dedup key — an identical event is already queued", err=True)
            raise typer.Exit(code=1)
        for handler in routing:
            await outbox.enqueue_handler(conn, event_id=event.id, handler=handler, now=now)
        await conn.commit()
    typer.echo(event.id)


@app.command("repl", help="Drop into IPython with the production container bound.")
def repl() -> None:
    raise NotImplementedError("Phase 3+: IPython.embed with container in scope")
