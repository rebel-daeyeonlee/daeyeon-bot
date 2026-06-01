"""Unit tests for `infra.gh_cli.GhCli` error mapping (T013).

Covers the contract from `contracts/github-api-surface.md` §"Auth & rate-limit
error contract":
    HTTP 401 → AuthError
    HTTP 403 + rate header → RateLimitError
    HTTP 422 on POST → PermanentError
    HTTP 5xx → TransientError
    HTTP 404 on GET → PermanentError
    Success → parsed JSON
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import patch

import pytest

from daeyeon_bot.core.errors import (
    AuthError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from daeyeon_bot.infra.gh_cli import GhCli


class _FakeProc:
    """Minimal async subprocess stand-in returning canned (rc, stdout, stderr)."""

    def __init__(self, *, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover - never reached in success paths
        pass


def _patch_subprocess(
    factory: Callable[..., Awaitable[_FakeProc]],
) -> AbstractContextManager[Any]:
    return patch("daeyeon_bot.infra.gh_cli.asyncio.create_subprocess_exec", new=factory)


def _ok_factory(payload: bytes) -> Callable[..., Awaitable[_FakeProc]]:
    async def factory(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=0, stdout=payload)

    return factory


def _err_factory(rc: int, stderr: bytes) -> Callable[..., Awaitable[_FakeProc]]:
    async def factory(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=rc, stderr=stderr)

    return factory


@pytest.mark.asyncio
async def test_success_path_returns_parsed_json() -> None:
    cli = GhCli()
    payload = json.dumps({"login": "alice"}).encode()
    with _patch_subprocess(_ok_factory(payload)):
        login = await cli.auth_user()
    assert login == "alice"


@pytest.mark.asyncio
async def test_http_401_raises_auth_error() -> None:
    cli = GhCli()
    stderr = b"HTTP 401: Bad credentials\n"
    with _patch_subprocess(_err_factory(1, stderr)):
        with pytest.raises(AuthError):
            await cli.pr_get("owner/repo", 1)


@pytest.mark.asyncio
async def test_http_403_rate_limit_raises_rate_limit_error() -> None:
    cli = GhCli()
    stderr = b"HTTP 403: API rate limit exceeded for user\nX-RateLimit-Remaining: 0\n"
    with _patch_subprocess(_err_factory(1, stderr)):
        with pytest.raises(RateLimitError):
            await cli.pr_get("owner/repo", 1)


@pytest.mark.asyncio
async def test_http_422_on_post_raises_permanent() -> None:
    cli = GhCli()
    stderr = b"HTTP 422: Validation failed\n"
    with _patch_subprocess(_err_factory(1, stderr)):
        with pytest.raises(PermanentError):
            await cli.post_review(
                "owner/repo",
                1,
                commit_id="abc123",
                body="Summary",
                comments=[],
            )


@pytest.mark.asyncio
async def test_http_500_raises_transient() -> None:
    cli = GhCli()
    stderr = b"HTTP 503: Service Unavailable\n"
    with _patch_subprocess(_err_factory(1, stderr)):
        with pytest.raises(TransientError):
            await cli.pr_get("owner/repo", 1)


@pytest.mark.asyncio
async def test_http_404_on_get_raises_permanent() -> None:
    cli = GhCli()
    stderr = b"HTTP 404: Not Found\n"
    with _patch_subprocess(_err_factory(1, stderr)):
        with pytest.raises(PermanentError):
            await cli.pr_get("owner/repo", 999)


@pytest.mark.asyncio
async def test_auth_phrase_in_stderr_raises_auth_error() -> None:
    cli = GhCli()
    stderr = b"could not refresh oauth token; authentication failed\n"
    with _patch_subprocess(_err_factory(1, stderr)):
        with pytest.raises(AuthError):
            await cli.auth_status()


@pytest.mark.asyncio
async def test_timeout_raises_transient() -> None:
    cli = GhCli(timeout_seconds=0.05)

    async def hanging_proc(*_args: Any, **_kwargs: Any) -> _FakeProc:
        class _Hang(_FakeProc):
            async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
                await asyncio.sleep(10)
                return b"", b""

        return _Hang(returncode=0)

    with _patch_subprocess(hanging_proc):
        with pytest.raises(TransientError):
            await cli.auth_user()


@pytest.mark.asyncio
async def test_post_review_returns_parsed_response() -> None:
    cli = GhCli()
    payload = json.dumps(
        {
            "id": 9876543,
            "submitted_at": "2026-05-04T14:31:02Z",
            "state": "COMMENTED",
        }
    ).encode()
    with _patch_subprocess(_ok_factory(payload)):
        out = await cli.post_review(
            "owner/repo",
            42,
            commit_id="abc123",
            body="Summary",
            comments=[{"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}],
        )
    assert out["id"] == 9876543
    assert out["state"] == "COMMENTED"


@pytest.mark.asyncio
async def test_search_review_requested_empty_username_raises() -> None:
    cli = GhCli()
    with pytest.raises(PermanentError):
        await cli.search_review_requested("")


@pytest.mark.asyncio
async def test_search_review_requested_parses_items() -> None:
    cli = GhCli()
    payload = json.dumps(
        {
            "total_count": 1,
            "items": [
                {
                    "number": 42,
                    "repository_url": "https://api.github.com/repos/owner/repo",
                    "pull_request": {
                        "url": "https://api.github.com/repos/owner/repo/pulls/42",
                        "draft": False,
                    },
                }
            ],
        }
    ).encode()
    with _patch_subprocess(_ok_factory(payload)):
        items = await cli.search_review_requested("alice")
    assert len(items) == 1
    assert items[0]["number"] == 42


@pytest.mark.asyncio
async def test_search_authored_empty_username_raises() -> None:
    cli = GhCli()
    with pytest.raises(PermanentError):
        await cli.search_authored("")


@pytest.mark.asyncio
async def test_search_authored_builds_author_query_and_parses_items() -> None:
    cli = GhCli()
    captured: list[tuple[str, ...]] = []

    async def factory(*args: Any, **_kwargs: Any) -> _FakeProc:
        captured.append(tuple(str(a) for a in args))
        return _FakeProc(
            returncode=0,
            stdout=json.dumps(
                {"items": [{"number": 7, "repository_url": "https://api.github.com/repos/o/r"}]}
            ).encode(),
        )

    with _patch_subprocess(factory):
        items = await cli.search_authored("daeyeon-lee", extra_query="user:rebellions-sw")

    assert len(items) == 1
    assert items[0]["number"] == 7
    flat = " ".join(captured[0])
    assert "q=is:open is:pr author:daeyeon-lee archived:false user:rebellions-sw" in flat


@pytest.mark.asyncio
async def test_auth_user_returns_login() -> None:
    cli = GhCli()
    with _patch_subprocess(_ok_factory(json.dumps({"login": "daeyeon-lee"}).encode())):
        login = await cli.auth_user()
    assert login == "daeyeon-lee"


@pytest.mark.asyncio
async def test_auth_user_missing_login_raises_permanent() -> None:
    cli = GhCli()
    with _patch_subprocess(_ok_factory(json.dumps({"login": ""}).encode())):
        with pytest.raises(PermanentError):
            await cli.auth_user()


@pytest.mark.asyncio
async def test_search_returns_empty_when_payload_is_not_dict() -> None:
    cli = GhCli()
    with _patch_subprocess(_ok_factory(json.dumps(["unexpected"]).encode())):
        items = await cli.search_review_requested("alice")
    assert items == []


@pytest.mark.asyncio
async def test_pr_get_non_object_raises_permanent() -> None:
    cli = GhCli()
    with _patch_subprocess(_ok_factory(json.dumps(["not-an-object"]).encode())):
        with pytest.raises(PermanentError):
            await cli.pr_get("o/r", 1)


@pytest.mark.asyncio
async def test_pr_files_returns_dict_items_only() -> None:
    cli = GhCli()
    payload = json.dumps([{"filename": "a.py"}, "garbage", {"filename": "b.py"}]).encode()
    with _patch_subprocess(_ok_factory(payload)):
        files = await cli.pr_files("o/r", 1)
    assert [f["filename"] for f in files] == ["a.py", "b.py"]


@pytest.mark.asyncio
async def test_pr_files_returns_empty_for_non_list_payload() -> None:
    cli = GhCli()
    with _patch_subprocess(_ok_factory(json.dumps({"unexpected": True}).encode())):
        files = await cli.pr_files("o/r", 1)
    assert files == []


@pytest.mark.asyncio
async def test_post_review_non_object_raises_permanent() -> None:
    cli = GhCli()
    with _patch_subprocess(_ok_factory(json.dumps([1, 2, 3]).encode())):
        with pytest.raises(PermanentError):
            await cli.post_review("o/r", 1, commit_id="abc", body="b", comments=[])


@pytest.mark.asyncio
async def test_empty_stdout_returns_empty_dict() -> None:
    cli = GhCli()
    with _patch_subprocess(_ok_factory(b"   \n   ")):
        out = await cli.pr_get("o/r", 1)
    assert out == {}


@pytest.mark.asyncio
async def test_post_review_5xx_dedups_via_reviews_list() -> None:
    """If the POST returns 5xx but the row exists, return the matching review.

    GitHub occasionally accepts the POST then 502/503s on the response leg —
    a naive retry would post a duplicate review. With `login` provided, the
    wrapper probes `GET /pulls/{n}/reviews`, filters by `(commit_id, login,
    submitted_at != null)`, and returns the existing row instead of raising.
    """
    cli = GhCli()
    matching_review = {
        "id": 12345,
        "commit_id": "abc123",
        "submitted_at": "2026-05-04T14:31:02Z",
        "user": {"login": "daeyeon-bot"},
        "state": "COMMENTED",
    }
    other_review = {
        "id": 999,
        "commit_id": "older_sha",
        "submitted_at": "2026-04-01T00:00:00Z",
        "user": {"login": "daeyeon-bot"},
    }
    pending_review = {
        "id": 998,
        "commit_id": "abc123",
        "submitted_at": None,
        "user": {"login": "daeyeon-bot"},
    }

    async def factory(*args: Any, **_kwargs: Any) -> _FakeProc:
        if "POST" in args:
            return _FakeProc(returncode=1, stderr=b"HTTP 502: Bad Gateway\n")
        # GET /reviews — return the candidate list.
        body = json.dumps([other_review, pending_review, matching_review]).encode()
        return _FakeProc(returncode=0, stdout=body)

    with _patch_subprocess(factory):
        out = await cli.post_review(
            "owner/repo",
            42,
            commit_id="abc123",
            body="Summary",
            comments=[],
            login="daeyeon-bot",
        )
    assert out["id"] == 12345
    assert out["submitted_at"] == "2026-05-04T14:31:02Z"


@pytest.mark.asyncio
async def test_post_review_5xx_without_login_propagates() -> None:
    """Without `login`, the wrapper can't dedup — TransientError must propagate."""
    cli = GhCli()
    with _patch_subprocess(_err_factory(1, b"HTTP 502: Bad Gateway\n")):
        with pytest.raises(TransientError):
            await cli.post_review(
                "owner/repo",
                42,
                commit_id="abc123",
                body="Summary",
                comments=[],
            )


@pytest.mark.asyncio
async def test_post_review_5xx_no_match_propagates() -> None:
    """If the dedup probe finds nothing, the original TransientError propagates."""
    cli = GhCli()

    async def factory(*args: Any, **_kwargs: Any) -> _FakeProc:
        if "POST" in args:
            return _FakeProc(returncode=1, stderr=b"HTTP 503: Service Unavailable\n")
        return _FakeProc(returncode=0, stdout=json.dumps([]).encode())

    with _patch_subprocess(factory):
        with pytest.raises(TransientError):
            await cli.post_review(
                "owner/repo",
                42,
                commit_id="abc123",
                body="Summary",
                comments=[],
                login="daeyeon-bot",
            )


@pytest.mark.asyncio
async def test_gh_cli_missing_raises_permanent() -> None:
    """If `gh` isn't on PATH, FileNotFoundError → PermanentError."""

    async def _missing(*_args: Any, **_kwargs: Any) -> _FakeProc:
        raise FileNotFoundError("gh: command not found")

    cli = GhCli()
    with _patch_subprocess(_missing):
        with pytest.raises(PermanentError):
            await cli.auth_status()
