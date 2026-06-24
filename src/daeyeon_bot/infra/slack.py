"""Async wrapper around the Slack Web API (feature 003).

Only the methods the CI-triage feature needs: `auth.test` (boot probe),
`conversations.history` (cursor poll), `conversations.replies` (thread context),
`chat.postMessage` (the single write — thread reply / dry_run, with
`chat:write.customize` identity). Uses `httpx` directly (same convention as
`infra/loki.py` / `infra/jira_client.py`) — NO `slack_sdk`, and the Slack MCP is
NOT used on the hot path (it is interactive-only, cannot run headless).

The token is a bot token (`xoxb-`, the existing `dev_syssw_test` bot) injected by
`app/container.py`. The user-token path is dead (workspace policy). See
specs/003-ci-monitor-bot/plan.md §infra/slack.py.

Write guard: `post_message` refuses any channel outside `allowed_post_channels`
(the 2 watched channels + dry_run channel), so `chat:write.public` cannot be
abused by a misconfigured target. The boot-time allowlist validation in
`container.build` is the primary guard; this is belt-and-suspenders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

from daeyeon_bot.core.errors import (
    AuthError,
    ConfigError,
    PermanentError,
    RateLimitError,
    TransientError,
)

_DEFAULT_TIMEOUT = 20.0
_AUTH_ERRORS = frozenset(
    {"invalid_auth", "not_authed", "token_revoked", "account_inactive", "token_expired"}
)


@dataclass(frozen=True, slots=True)
class SlackIdentity:
    """Result of `auth.test`."""

    team: str | None
    user: str | None
    user_id: str | None
    bot_id: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of `conversations.history`."""

    messages: list[dict[str, Any]]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PostResult:
    """Result of `chat.postMessage`."""

    channel: str
    ts: str


class SlackClient:
    """Thin httpx wrapper. One instance per daemon."""

    def __init__(
        self,
        *,
        token: str,
        allowed_post_channels: frozenset[str],
        api_base: str = "https://slack.com/api",
        timeout_s: float = _DEFAULT_TIMEOUT,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._allowed = allowed_post_channels
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout_s
        self._http = http

    async def auth_test(self) -> SlackIdentity:
        data = await self._call("auth.test", {})
        return SlackIdentity(
            team=_opt_str(data.get("team")),
            user=_opt_str(data.get("user")),
            user_id=_opt_str(data.get("user_id")),
            bot_id=_opt_str(data.get("bot_id")),
            url=_opt_str(data.get("url")),
        )

    async def history(
        self,
        channel_id: str,
        *,
        oldest: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        params: dict[str, str] = {"channel": channel_id, "limit": str(limit)}
        if oldest is not None:
            params["oldest"] = oldest
            params["inclusive"] = "false"
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._call("conversations.history", params)
        return HistoryPage(messages=_messages(data), next_cursor=_next_cursor(data))

    async def replies(
        self,
        channel_id: str,
        *,
        thread_ts: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {"channel": channel_id, "ts": thread_ts, "limit": str(limit)}
        data = await self._call("conversations.replies", params)
        return _messages(data)

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        username: str | None = None,
        icon_emoji: str | None = None,
    ) -> PostResult:
        """The only write. Channel-allowlist guarded — refuses any target outside
        the configured set (ConfigError → DeadLetter at handler time; the boot
        guard catches misconfig before any alert is processed)."""
        if channel_id not in self._allowed:
            raise ConfigError(
                f"slack: refusing to post to {channel_id!r} — not in post allowlist"
                f" {sorted(self._allowed)}"
            )
        params: dict[str, str] = {"channel": channel_id, "text": text}
        if thread_ts is not None:
            params["thread_ts"] = thread_ts
        if username is not None:
            params["username"] = username
        if icon_emoji is not None:
            params["icon_emoji"] = icon_emoji
        data = await self._call("chat.postMessage", params)
        return PostResult(
            channel=_opt_str(data.get("channel")) or channel_id,
            ts=_opt_str(data.get("ts")) or "",
        )

    async def message_reactions(self, channel_id: str, timestamp: str) -> list[tuple[str, int]]:
        """Reactions on one message as (emoji_name, count) — feature 003 D
        feedback loop. The bot's triage is a THREAD REPLY, which
        `conversations.history` does not return; `conversations.replies` with the
        message's own ts does (and works for a top-level message too). Reads the
        `reactions` field off it — scope `channels:history`/`groups:history`
        (held), not the separate `reactions:read`. Empty when none."""
        data = await self._call(
            "conversations.replies",
            {"channel": channel_id, "ts": timestamp, "limit": "1"},
        )
        messages = _messages(data)
        target = next((m for m in messages if str(m.get("ts")) == timestamp), None)
        if target is None:
            return []
        reactions = target.get("reactions")
        out: list[tuple[str, int]] = []
        if isinstance(reactions, list):
            for r in cast("list[Any]", reactions):
                if not isinstance(r, dict):
                    continue
                rd = cast("dict[str, Any]", r)
                name = rd.get("name")
                if isinstance(name, str) and name:
                    count = rd.get("count")
                    out.append((name, int(count) if isinstance(count, int) else 1))
        return out

    # ── plumbing ────────────────────────────────────────────────────────────────

    async def _call(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self._api_base}/{method}"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            if self._http is not None:
                resp = await self._http.post(
                    url, data=params, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, data=params, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"slack {method}: transport error: {exc}") from exc

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "1")
            raise RateLimitError(f"slack {method}: rate-limited (Retry-After={retry_after})")
        if 500 <= resp.status_code < 600:
            raise TransientError(f"slack {method}: HTTP {resp.status_code}")
        try:
            payload_obj: object = resp.json()
        except ValueError as exc:
            raise TransientError(f"slack {method}: non-JSON response") from exc
        if not isinstance(payload_obj, dict):
            raise PermanentError(f"slack {method}: response is not an object")
        payload = cast("dict[str, Any]", payload_obj)
        if payload.get("ok") is True:
            return payload

        error = _opt_str(payload.get("error")) or "unknown"
        if error in _AUTH_ERRORS:
            raise AuthError(f"slack {method}: auth rejected: {error}")
        if error == "ratelimited":
            raise RateLimitError(f"slack {method}: ratelimited")
        raise PermanentError(f"slack {method}: {error}")


def _messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("messages")
    if not isinstance(raw, list):
        return []
    return [cast("dict[str, Any]", m) for m in cast("list[Any]", raw) if isinstance(m, dict)]


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _next_cursor(data: dict[str, Any]) -> str | None:
    meta = data.get("response_metadata")
    if isinstance(meta, dict):
        cur = cast("dict[str, Any]", meta).get("next_cursor")
        if isinstance(cur, str) and cur:
            return cur
    return None


__all__ = [
    "HistoryPage",
    "PostResult",
    "SlackClient",
    "SlackIdentity",
]
