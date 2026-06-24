"""Minimal async Linear GraphQL client — issue search only (feature 003 P2).

ci_triage uses this to surface already-open DOLIN issues that match a failing
host / signature ("이거 이미 티켓 있음"). Search-only and best-effort: every
public method returns [] on any error (auth, network, schema drift), because a
missing ticket link must never fail a triage. Auth is a Linear personal API key
(secret `linear_api_token`), sent verbatim in the `Authorization` header (Linear
keys are not Bearer-prefixed).

This is a deliberately tiny surface (one GraphQL query); it is NOT the full Jira
client analogue. If Linear write/sync is ever needed, grow it then.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

_ENDPOINT = "https://api.linear.app/graphql"
_DEFAULT_TIMEOUT = 15.0

# `searchIssues(term:)` is Linear's full-text issue search; `nodes` carry the
# identifier/title/url/state we render. Parsed defensively so a schema change
# degrades to [] rather than raising.
_SEARCH_QUERY = """
query CiTriageSearch($term: String!, $first: Int!) {
  searchIssues(term: $term, first: $first) {
    nodes { identifier title url state { name type } }
  }
}
"""


@dataclass(frozen=True, slots=True)
class LinearIssue:
    identifier: str  # e.g. "DOLIN-2207"
    title: str
    url: str
    state_name: str
    state_type: str  # backlog | unstarted | started | completed | canceled

    @property
    def is_open(self) -> bool:
        return self.state_type not in ("completed", "canceled")


class LinearClient:
    """Tiny httpx wrapper around the Linear GraphQL search. One per daemon."""

    def __init__(
        self,
        *,
        api_token: str,
        timeout_s: float = _DEFAULT_TIMEOUT,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = api_token
        self._timeout = timeout_s
        self._http = http  # caller-injected (tests); else built per-request

    async def search_issues(self, term: str, *, limit: int = 3) -> list[LinearIssue]:
        """Full-text issue search. Returns [] on empty term or ANY error."""
        if not term.strip() or not self._token:
            return []
        payload = {"query": _SEARCH_QUERY, "variables": {"term": term, "first": limit}}
        headers = {"Authorization": self._token, "Content-Type": "application/json"}
        try:
            if self._http is not None:
                resp = await self._http.post(_ENDPOINT, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(_ENDPOINT, json=payload, headers=headers)
            if resp.status_code != 200:
                return []
            data: object = resp.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return []
        return _parse_nodes(data)


def _obj(value: object) -> dict[str, Any]:
    """Narrow untrusted JSON to a str-keyed dict (empty when not a dict)."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _parse_nodes(data: object) -> list[LinearIssue]:
    """Pull issues out of a GraphQL response, dropping anything malformed."""
    nodes = _obj(_obj(_obj(data).get("data")).get("searchIssues")).get("nodes")
    if not isinstance(nodes, list):
        return []
    out: list[LinearIssue] = []
    for node in cast("list[Any]", nodes):
        nd = _obj(node)
        ident = nd.get("identifier")
        if not isinstance(ident, str) or not ident:
            continue
        state = _obj(nd.get("state"))
        out.append(
            LinearIssue(
                identifier=ident,
                title=str(nd.get("title") or ""),
                url=str(nd.get("url") or ""),
                state_name=str(state.get("name") or ""),
                state_type=str(state.get("type") or ""),
            )
        )
    return out


__all__ = ["LinearClient", "LinearIssue"]
