"""SQLite storage adapter (aiosqlite + WAL pragma).

Connection factory enforces our PRAGMA contract on every connection. Migrations
are linear, additive files in `infra/db/migrations/NNN_*.sql`. The `meta.schema_version`
row is the only persisted indicator of applied state.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite

PRAGMA_BOOTSTRAP = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA foreign_keys=ON;",
)

_MIGRATION_NAME_RE = re.compile(r"^(\d{3})_[\w_]+\.sql$")


def _resolve_path(p: str | Path) -> Path:
    return Path(p).expanduser().resolve()


async def open_db(path: str | Path) -> aiosqlite.Connection:
    """Return an opened aiosqlite connection with our PRAGMA contract applied."""
    target = _resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(target)
    conn.row_factory = aiosqlite.Row
    for pragma in PRAGMA_BOOTSTRAP:
        await conn.execute(pragma)
    await conn.commit()
    return conn


@asynccontextmanager
async def connection(path: str | Path) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager mirroring `open_db` for short-lived users (CLI subcommands)."""
    conn = await open_db(path)
    try:
        yield conn
    finally:
        await conn.close()


def _migration_files() -> list[tuple[int, str, str]]:
    """Return [(seq, filename, sql), ...] sorted by sequence number."""
    pkg = resources.files("daeyeon_bot.infra.db.migrations")
    found: list[tuple[int, str, str]] = []
    for entry in pkg.iterdir():
        name = entry.name
        match = _MIGRATION_NAME_RE.match(name)
        if not match:
            continue
        seq = int(match.group(1))
        sql = entry.read_text(encoding="utf-8")
        found.append((seq, name, sql))
    found.sort(key=lambda item: item[0])
    return found


async def _current_schema_version(conn: aiosqlite.Connection) -> int:
    """Return the integer schema_version, or 0 when meta is absent."""
    try:
        async with conn.execute("SELECT value FROM meta WHERE key = 'schema_version'") as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(row["value"]) if row is not None else 0


async def apply_migrations(conn: aiosqlite.Connection) -> int:
    """Apply all migration files whose sequence number > current schema_version.

    Each migration runs inside its own transaction. Returns the schema_version
    after the run.
    """
    migrations = _migration_files()
    if not migrations:
        return await _current_schema_version(conn)

    current = await _current_schema_version(conn)
    for seq, _name, sql in migrations:
        if seq <= current:
            continue
        await conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        await conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(seq),),
        )
        await conn.commit()
        current = seq
    return current


async def fetch_one(
    conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    """Convenience: fetch a single row as a plain dict, or None."""
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None
