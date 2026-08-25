"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

from daeyeon_bot.infra import storage


async def wait_for_migrated_db(db_path: Path, *, timeout_s: float = 10.0) -> None:
    """Block until a booting daemon has finished migrating `db_path`.

    Integration tests that drive `lifecycle.boot()` need the schema in place
    before they can insert an event. The obvious wait — poll until the file
    exists — is wrong: SQLite creates the file at `open_db`, well before
    `apply_migrations` (boot step 5) has run. A test that proceeds on the file
    alone then calls `apply_migrations` on its own connection and RACES the
    daemon's, and because SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT
    EXISTS`, migration 007 blows up with "duplicate column name". Waiting for
    the version to reach head removes the race and makes the test's own
    `apply_migrations` call unnecessary.
    """
    head = max((seq for seq, _name, _sql in storage.migration_files()), default=0)
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if db_path.exists():
            try:
                async with storage.connection(db_path) as conn:
                    async with conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ) as cur:
                        row = await cur.fetchone()
                if row is not None and int(row["value"]) >= head:
                    return
            except Exception:
                # The daemon may be mid-migration; `meta` can be momentarily
                # absent or locked. Keep polling until the deadline.
                pass
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"database at {db_path} did not reach schema_version {head} in {timeout_s}s"
    )


__all__ = ["wait_for_migrated_db"]
