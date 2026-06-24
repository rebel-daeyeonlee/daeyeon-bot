"""Linear GraphQL search client (feature 003 P2)."""

from __future__ import annotations

from typing import Any

from daeyeon_bot.infra.linear_client import LinearClient, _parse_nodes


def _resp(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"searchIssues": {"nodes": nodes}}}


def test_parse_nodes_extracts_issues() -> None:
    out = _parse_nodes(
        _resp(
            [
                {
                    "identifier": "DOLIN-2207",
                    "title": "legacy 패키지 코드 수정",
                    "url": "https://linear.app/x/DOLIN-2207",
                    "state": {"name": "In Progress", "type": "started"},
                }
            ]
        )
    )
    assert len(out) == 1
    assert out[0].identifier == "DOLIN-2207"
    assert out[0].is_open is True


def test_parse_nodes_open_vs_closed() -> None:
    out = _parse_nodes(
        _resp(
            [
                {"identifier": "A-1", "state": {"name": "Done", "type": "completed"}},
                {"identifier": "A-2", "state": {"name": "Canceled", "type": "canceled"}},
                {"identifier": "A-3", "state": {"name": "Todo", "type": "unstarted"}},
            ]
        )
    )
    assert [i.identifier for i in out if i.is_open] == ["A-3"]


def test_parse_nodes_tolerates_garbage() -> None:
    assert _parse_nodes(None) == []
    assert _parse_nodes({"data": {}}) == []
    assert _parse_nodes({"data": {"searchIssues": {"nodes": "nope"}}}) == []
    assert _parse_nodes(_resp([{"no_identifier": 1}, "junk"])) == []  # type: ignore[list-item]


class _FakeResp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttp:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResp:
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._resp


async def test_search_issues_happy_path() -> None:
    http = _FakeHttp(_FakeResp(_resp([{"identifier": "DOLIN-1", "state": {"type": "started"}}])))
    client = LinearClient(api_token="lin_xxx", http=http)  # type: ignore[arg-type]
    issues = await client.search_issues("ssw-smci-16", limit=3)
    assert [i.identifier for i in issues] == ["DOLIN-1"]
    assert http.calls[0]["headers"]["Authorization"] == "lin_xxx"
    assert http.calls[0]["json"]["variables"] == {"term": "ssw-smci-16", "first": 3}


async def test_search_issues_guards() -> None:
    client = LinearClient(api_token="lin_xxx")
    assert await client.search_issues("   ") == []
    assert await LinearClient(api_token="").search_issues("term") == []


async def test_search_issues_non_200_returns_empty() -> None:
    http = _FakeHttp(_FakeResp({}, status=401))
    client = LinearClient(api_token="lin_xxx", http=http)  # type: ignore[arg-type]
    assert await client.search_issues("term") == []
