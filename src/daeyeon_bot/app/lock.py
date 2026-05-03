"""pidfile + flock(2) for single-instance enforcement.

Held for the daemon's lifetime. The lock is released by close() (which the
finally branch in lifecycle.boot always reaches) and the pidfile is unlinked
at the same time. A crashed process leaves the pidfile behind but the OS
drops the flock, so the next start re-acquires cleanly.
"""

from __future__ import annotations

import errno
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

from daeyeon_bot.core.errors import BotError


class AlreadyRunningError(BotError):
    """Another daeyeon-bot instance holds the pidfile lock."""

    def __init__(self, path: Path, holder_pid: int | None) -> None:
        self.path = path
        self.holder_pid = holder_pid
        suffix = f" (pid {holder_pid})" if holder_pid is not None else ""
        super().__init__(f"another daeyeon-bot instance is running{suffix}: {path}")


@dataclass(slots=True)
class PidLock:
    """Owns a pidfile + advisory lock. Use `acquire()` / `close()`.

    flock(2) is per-fd so the file descriptor must outlive every consumer of
    the lock. We keep it in `_fd` and never expose it.
    """

    path: Path
    _fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_RDWR so we can both lock and rewrite the pid; O_CREAT so a missing
        # file is harmless. We deliberately do not unlink first — that would
        # break the holder's lock.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise AlreadyRunningError(self.path, _read_pid(self.path)) from exc
            raise

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd

    def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            # Best-effort unlink before releasing — keeps the directory tidy
            # under normal exits without breaking the contract on crash.
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


__all__ = ["AlreadyRunningError", "PidLock"]
