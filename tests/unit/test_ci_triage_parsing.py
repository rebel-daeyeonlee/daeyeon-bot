"""ANSI strip + error-anchored truncation (feature 003)."""

from __future__ import annotations

from daeyeon_bot.handlers.ci_triage_parsing import error_anchored_windows, strip_ansi


def test_strip_ansi_removes_color_codes() -> None:
    raw = "\x1b[36;1mecho hi\x1b[0m"
    assert strip_ansi(raw) == "echo hi"


def test_anchored_windows_capture_error_not_head() -> None:
    # Head is GITHUB_TOKEN boilerplate; the real cause is buried mid-log.
    head = "\n".join(
        f"job / step\tUNKNOWN STEP\t2026-06-17T02:10:{i:02d}.0Z Permissions line {i}"
        for i in range(40)
    )
    cause = (
        "premerge / result\tUNKNOWN STEP\t2026-06-17T02:20:31.0Z "
        'rsync: link_stat "/mnt/data/qemu/images/base/ubuntu-22.04-golden-base" '
        "failed: No such file or directory\n"
        "premerge / result\tUNKNOWN STEP\t2026-06-17T02:20:41.8Z ##[error]Process completed with exit code 1."
    )
    out = error_anchored_windows(head + "\n" + cause, context=3)
    assert "golden-base" in out
    assert "Process completed with exit code 1" in out
    # Boilerplate head should be excluded (no anchor near it).
    assert "Permissions line 5" not in out


def test_small_log_without_anchor_returned_cleaned() -> None:
    raw = "job\tstep\t2026-06-17T00:00:00.0Z just a note line"
    out = error_anchored_windows(raw)
    assert "just a note line" in out
    # ts prefix stripped, job tag kept.
    assert "2026-06-17T00:00:00" not in out


def test_max_chars_cap() -> None:
    big = "\n".join(
        f"j\ts\t2026-06-17T00:00:{i % 60:02d}.0Z ##[error]boom {i}" for i in range(5000)
    )
    out = error_anchored_windows(big, context=1, max_chars=2000)
    assert len(out) <= 2000 + 32  # cap + the truncation marker
