"""`gh run view --log-failed` + its dedicated non-`api` classifier (feature 003).

The key regression: a gone/expired run log (no HTTP code in stderr) must map to
RunLogUnavailableError (→ skip), NOT TransientError (→ infinite Retry).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from daeyeon_bot.core.errors import AuthError, RunLogUnavailableError, TransientError
from daeyeon_bot.infra.gh_cli import (
    FailedJob,
    GhCli,
    _GhResult,  # pyright: ignore[reportPrivateUsage]
)


def _stub_run(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> Callable[..., object]:
    async def _run(*_args: str, stdin: bytes | None = None) -> _GhResult:
        return _GhResult(returncode=returncode, stdout=stdout, stderr=stderr)

    return _run


async def test_success_returns_log_text() -> None:
    gh = GhCli()
    gh._run = _stub_run(0, stdout=b"job\tSTEP\t##[error]boom\n")  # type: ignore[method-assign]
    out = await gh.run_view_log_failed("rebellions-sw/ssw-bundle", "123")
    assert "##[error]boom" in out


async def test_not_found_maps_to_run_log_unavailable() -> None:
    gh = GhCli()
    gh._run = _stub_run(  # type: ignore[method-assign]
        1, stderr=b"could not find any workflow run with ID 123"
    )
    with pytest.raises(RunLogUnavailableError):
        await gh.run_view_log_failed("rebellions-sw/ssw-bundle", "123")


async def test_logs_expired_maps_to_run_log_unavailable() -> None:
    gh = GhCli()
    gh._run = _stub_run(1, stderr=b"failed to get run log: logs expired")  # type: ignore[method-assign]
    with pytest.raises(RunLogUnavailableError):
        await gh.run_view_log_failed("rebellions-sw/ssw-bundle", "123")


async def test_auth_stderr_still_maps_to_auth_error() -> None:
    gh = GhCli()
    gh._run = _stub_run(1, stderr=b"gh: bad credentials")  # type: ignore[method-assign]
    with pytest.raises(AuthError):
        await gh.run_view_log_failed("rebellions-sw/ssw-bundle", "123")


async def test_http_5xx_maps_to_transient() -> None:
    gh = GhCli()
    gh._run = _stub_run(1, stderr=b"HTTP 503: service unavailable")  # type: ignore[method-assign]
    with pytest.raises(TransientError):
        await gh.run_view_log_failed("rebellions-sw/ssw-bundle", "123")


async def test_unknown_nonzero_maps_to_transient() -> None:
    gh = GhCli()
    gh._run = _stub_run(1, stderr=b"connection reset by peer")  # type: ignore[method-assign]
    with pytest.raises(TransientError):
        await gh.run_view_log_failed("rebellions-sw/ssw-bundle", "123")


# ── run_failed_job_logs: per-job fetch with individual 404 tolerance ──────────


async def test_failed_job_logs_tolerates_individual_404() -> None:
    gh = GhCli()

    async def _failed_jobs(repo: str, run_id: str) -> list[FailedJob]:
        return [
            FailedJob("111", "ci-test (matrix leaf)", "ssw-pc-21", None, None),
            FailedJob("222", "result", "ssw-hp-01", None, None),
        ]

    async def _job_logs(repo: str, job_id: str) -> str | None:
        return None if job_id == "111" else "Atom test failed\n##[error]exit 1"

    gh._failed_jobs = _failed_jobs  # type: ignore[method-assign]
    gh._job_logs = _job_logs  # type: ignore[method-assign]
    out = await gh.run_failed_job_logs("rebellions-sw/ssw-bundle", "27800853109")
    assert "result" in out
    assert "Atom test failed" in out
    assert "ci-test (matrix leaf)" not in out  # the 404'd job is skipped


async def test_failed_job_logs_all_404_raises() -> None:
    gh = GhCli()

    async def _failed_jobs(repo: str, run_id: str) -> list[FailedJob]:
        return [
            FailedJob("111", "a", None, None, None),
            FailedJob("222", "b", None, None, None),
        ]

    async def _job_logs(repo: str, job_id: str) -> str | None:
        return None

    gh._failed_jobs = _failed_jobs  # type: ignore[method-assign]
    gh._job_logs = _job_logs  # type: ignore[method-assign]
    with pytest.raises(RunLogUnavailableError):
        await gh.run_failed_job_logs("rebellions-sw/ssw-bundle", "27800853109")
