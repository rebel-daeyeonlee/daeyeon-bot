"""Lifecycle commands: run / pause / resume / stop."""

from __future__ import annotations

import asyncio
import os
import signal

import typer

from daeyeon_bot.app.config import load
from daeyeon_bot.app.lifecycle import AlreadyRunningError, BootOptions, boot

app = typer.Typer(help="Lifecycle controls: pause, resume, stop.", no_args_is_help=True)


def run(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    """Start the daemon (foreground). Use launchd / systemd in production."""
    try:
        asyncio.run(boot(BootOptions(config_path=config)))
    except AlreadyRunningError as exc:
        typer.echo(f"daeyeon-bot: {exc}", err=True)
        # 75 = EX_TEMPFAIL — supervisor (launchd/systemd) should retry later.
        raise typer.Exit(code=75) from exc


@app.command("pause", help="Create the PAUSE flag; running handlers continue, new ones block.")
def pause() -> None:
    raise NotImplementedError("Phase 3: touch PAUSE file in state_dir")


@app.command("resume", help="Remove the PAUSE flag; new handlers may proceed.")
def resume() -> None:
    raise NotImplementedError("Phase 3: unlink PAUSE file in state_dir")


@app.command("stop", help="Send SIGTERM to the running daemon (pidfile lookup).")
def stop(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config.toml."),
) -> None:
    cfg = load(config)
    pidfile = cfg.pidfile_path
    if not pidfile.exists():
        typer.echo(f"no pidfile at {pidfile}", err=True)
        raise typer.Exit(code=1)
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        typer.echo(f"unreadable pidfile {pidfile}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"no process with pid {pid} (stale pidfile?)", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"SIGTERM sent to pid {pid}")
