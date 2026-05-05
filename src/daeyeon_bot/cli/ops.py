"""Ops commands: doctor / migrate / replay / prune / backup."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import typer

from daeyeon_bot.app.backup import BackupReport, run_backup
from daeyeon_bot.app.config import load, resolve_config_path
from daeyeon_bot.app.doctor import DoctorReport, run_checks
from daeyeon_bot.app.prune import PruneReport
from daeyeon_bot.app.prune import prune as run_prune
from daeyeon_bot.app.replay import ReplayPlan, plan_replay
from daeyeon_bot.app.replay import replay as run_replay
from daeyeon_bot.infra import storage

app = typer.Typer(
    help="Operations: pre-flight checks, schema migrations, replay, prune, backup.",
    no_args_is_help=True,
)


@app.command(
    "doctor",
    help="Run pre-flight checks: state dir, disk, heartbeat, PAUSE, DB integrity, schema, token.",
)
def doctor(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    resolved = resolve_config_path(config)
    if resolved is None:
        typer.echo("config: using defaults (no config.toml found)")
    else:
        typer.echo(f"config: {resolved}")
    report = asyncio.run(_doctor(config_path=config))
    _render_doctor(report)
    if not report.ok:
        raise typer.Exit(code=1)


async def _doctor(*, config_path: str | None) -> DoctorReport:
    cfg = load(config_path)
    return await run_checks(cfg)


def _render_doctor(report: DoctorReport) -> None:
    width = max(len(r.name) for r in report.results)
    for result in report.results:
        marker = {"ok": "✓", "warn": "!", "fail": "✗"}[result.status]
        typer.echo(f"{marker} {result.name.ljust(width)}  {result.detail}")


@app.command("migrate", help="Apply pending SQLite schema migrations under a transaction.")
def migrate(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    asyncio.run(_migrate(config_path=config))


async def _migrate(*, config_path: str | None) -> None:
    cfg = load(config_path)
    cfg.state_dir_path.mkdir(parents=True, exist_ok=True)
    async with storage.connection(cfg.db_path) as conn:
        version = await storage.apply_migrations(conn)
    typer.echo(f"schema_version={version}")


@app.command("replay", help="Re-emit a dead-lettered or processed event (attempt_epoch++).")
def replay(
    event_id: str,
    handler: str | None = typer.Option(None, "--handler", help="Replay only this handler."),
    confirm: bool = typer.Option(False, "--confirm", help="Required to actually re-emit."),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    plan = asyncio.run(
        _replay(event_id=event_id, handler=handler, confirm=confirm, config_path=config)
    )
    _render_replay(plan, confirmed=confirm)
    if plan.empty:
        raise typer.Exit(code=1)


async def _replay(
    *, event_id: str, handler: str | None, confirm: bool, config_path: str | None
) -> ReplayPlan:
    cfg = load(config_path)
    async with storage.connection(cfg.db_path) as conn:
        if not confirm:
            targets = await plan_replay(conn, event_id=event_id, handler=handler)
            return ReplayPlan(event_id=event_id, targets=targets, committed=False)
        return await run_replay(conn, event_id=event_id, handler=handler, now=datetime.now(tz=UTC))


def _render_replay(plan: ReplayPlan, *, confirmed: bool) -> None:
    if plan.empty:
        typer.echo(f"replay: no outbox rows match event {plan.event_id}", err=True)
        return
    verb = "would replay" if not confirmed else "replayed"
    typer.echo(f"{verb} {len(plan.targets)} row(s) for event {plan.event_id}:")
    for target in plan.targets:
        typer.echo(
            f"  outbox#{target.outbox_id}  handler={target.handler}"
            f"  status={target.current_status}  attempt_epoch={target.current_attempt_epoch}"
        )
    if not confirmed:
        typer.echo("dry-run; pass --confirm to actually re-queue.")


@app.command("prune", help="Apply retention: dedup expired keys + old runs (config-driven).")
def prune(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    report = asyncio.run(_prune(config_path=config))
    typer.echo(
        f"runs deleted: {report.runs_deleted}  "
        f"dedup_keys deleted: {report.dedup_keys_deleted}  "
        f"events deleted: {report.events_deleted}  "
        f"outbox deleted: {report.outbox_deleted}"
    )


async def _prune(*, config_path: str | None) -> PruneReport:
    cfg = load(config_path)
    async with storage.connection(cfg.db_path) as conn:
        return await run_prune(conn, config=cfg, now=datetime.now(tz=UTC))


@app.command("backup", help="Hot SQLite snapshot under <state_dir>/backups; prunes to backup_keep.")
def backup(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    report = asyncio.run(_backup(config_path=config))
    typer.echo(f"snapshot: {report.snapshot_path}")
    if report.pruned:
        typer.echo(f"pruned {len(report.pruned)} old backup(s)")


async def _backup(*, config_path: str | None) -> BackupReport:
    cfg = load(config_path)
    return await run_backup(
        db_path=cfg.db_path,
        state_dir=cfg.state_dir_path,
        keep=cfg.retention.backup_keep,
        now=datetime.now(tz=UTC),
    )
