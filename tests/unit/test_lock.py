"""PidLock: single-instance enforcement via fcntl.flock."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from daeyeon_bot.app.lock import AlreadyRunningError, PidLock


def test_acquire_writes_pid(tmp_path: Path) -> None:
    lock = PidLock(path=tmp_path / "daemon.pid")
    lock.acquire()
    try:
        assert lock.path.exists()
        assert int(lock.path.read_text().strip()) == os.getpid()
    finally:
        lock.close()
    assert not lock.path.exists()


def test_double_acquire_in_same_process_is_idempotent(tmp_path: Path) -> None:
    lock = PidLock(path=tmp_path / "daemon.pid")
    lock.acquire()
    try:
        lock.acquire()  # no-op, must not raise
        assert lock.path.exists()
    finally:
        lock.close()


def test_second_holder_in_separate_process_fails_fast(tmp_path: Path) -> None:
    """Spawn a child that holds the lock, then try to acquire it locally."""
    pidfile = tmp_path / "daemon.pid"
    ready = tmp_path / "child_ready"
    release = tmp_path / "child_release"

    script = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        from daeyeon_bot.app.lock import PidLock
        lock = PidLock(path=Path({str(pidfile)!r}))
        lock.acquire()
        Path({str(ready)!r}).write_text("ok")
        for _ in range(200):
            if Path({str(release)!r}).exists():
                break
            time.sleep(0.05)
        lock.close()
        """
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(200):
            if ready.exists():
                break
            if proc.poll() is not None:
                _, err = proc.communicate(timeout=1)
                pytest.fail(f"child exited early: {err.decode(errors='replace')}")
            time.sleep(0.05)
        else:
            proc.terminate()
            _, err = proc.communicate(timeout=2)
            pytest.fail(f"child never acquired the lock; stderr={err.decode(errors='replace')}")

        local = PidLock(path=pidfile)
        with pytest.raises(AlreadyRunningError) as info:
            local.acquire()
        assert info.value.holder_pid == proc.pid
    finally:
        release.write_text("go")
        proc.wait(timeout=5)


def test_acquire_after_holder_release_succeeds(tmp_path: Path) -> None:
    pidfile = tmp_path / "daemon.pid"
    a = PidLock(path=pidfile)
    a.acquire()
    a.close()

    b = PidLock(path=pidfile)
    b.acquire()
    try:
        assert pidfile.exists()
    finally:
        b.close()
