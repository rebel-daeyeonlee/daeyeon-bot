"""In-memory `gh` substitute for tests.

Mirrors the public method signatures of `daeyeon_bot.infra.gh_cli.GhCli` so
tests can swap `FakeGh` for the real wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from daeyeon_bot.core.errors import (
    AuthError,
    PermanentError,
    RateLimitError,
    TransientError,
)


@dataclass(slots=True)
class _FakePr:
    """Canned PR record."""

    repo: str
    pr_number: int
    head_sha: str
    author: str
    requested: tuple[str, ...]
    title: str
    body: str
    files: list[dict[str, Any]]
    draft: bool = False
    state: str = "open"


@dataclass(slots=True)
class FakeGh:
    """Fake GhCli backed by an in-memory dict of canned responses."""

    user_login: str = "daeyeon-lee"
    auth_ok: bool = True
    rate_limited: bool = False
    auth_user_raises: Exception | None = None
    post_review_response_id: int = 9876543
    raise_on_post: Exception | None = None
    raise_on_search: Exception | None = None
    # Captures the most recent `extra_query` arg `search_review_requested`
    # was called with, so trigger tests can assert the search-side
    # `allowed_repos` filter was actually plumbed through.
    last_extra_query: str = ""
    # Same, for the `author:<operator>` self-review search.
    last_authored_extra_query: str = ""

    _prs: dict[tuple[str, int], _FakePr] = field(default_factory=dict)
    _search_set: set[tuple[str, int]] = field(default_factory=set)
    _authored_set: set[tuple[str, int]] = field(default_factory=set)
    _posted_reviews: list[dict[str, Any]] = field(default_factory=list)
    _next_review_id: int = field(default=0)
    _prior_reviews: list[dict[str, Any]] = field(default_factory=list)

    # ── Helpers used by tests ────────────────────────────────────────────

    def add_pr(
        self,
        repo: str,
        pr_number: int,
        *,
        head_sha: str,
        author: str = "alice",
        requested: tuple[str, ...] = (),
        title: str = "Test PR",
        body: str = "",
        files: list[dict[str, Any]] | None = None,
        in_search_set: bool = True,
        in_authored_set: bool = False,
        draft: bool = False,
        state: str = "open",
    ) -> None:
        if not requested:
            requested = (self.user_login,)
        self._prs[(repo, pr_number)] = _FakePr(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            author=author,
            requested=requested,
            title=title,
            body=body,
            files=list(files) if files is not None else [],
            draft=draft,
            state=state,
        )
        if in_search_set:
            self._search_set.add((repo, pr_number))
        else:
            self._search_set.discard((repo, pr_number))
        if in_authored_set:
            self._authored_set.add((repo, pr_number))
        else:
            self._authored_set.discard((repo, pr_number))

    def update_head_sha(self, repo: str, pr_number: int, *, head_sha: str) -> None:
        pr = self._prs[(repo, pr_number)]
        self._prs[(repo, pr_number)] = _FakePr(
            repo=pr.repo,
            pr_number=pr.pr_number,
            head_sha=head_sha,
            author=pr.author,
            requested=pr.requested,
            title=pr.title,
            body=pr.body,
            files=pr.files,
            draft=pr.draft,
            state=pr.state,
        )

    def remove_from_search(self, repo: str, pr_number: int) -> None:
        self._search_set.discard((repo, pr_number))

    def add_to_search(self, repo: str, pr_number: int) -> None:
        if (repo, pr_number) in self._prs:
            self._search_set.add((repo, pr_number))

    def posted_reviews(self) -> list[dict[str, Any]]:
        return list(self._posted_reviews)

    # ── Public API mirroring GhCli ───────────────────────────────────────

    async def auth_status(self) -> None:
        if not self.auth_ok:
            raise AuthError("fake gh auth status: not logged in")

    async def auth_user(self) -> str:
        if self.auth_user_raises is not None:
            raise self.auth_user_raises
        if not self.auth_ok:
            raise AuthError("fake gh: not logged in")
        return self.user_login

    async def search_review_requested(
        self, username: str, *, extra_query: str = ""
    ) -> list[dict[str, Any]]:
        # Record the most recent extra_query so trigger tests can assert
        # the search-side filter actually reaches the gh layer. Filtering
        # is faked at the operator's request via `_search_set` directly.
        self.last_extra_query = extra_query
        if self.raise_on_search is not None:
            raise self.raise_on_search
        if not self.auth_ok:
            raise AuthError("fake gh search: not logged in")
        if self.rate_limited:
            raise RateLimitError("fake gh: rate limited")
        if not username:
            raise PermanentError("fake gh: empty username")
        return self._search_items(self._search_set)

    async def search_authored(
        self, username: str, *, extra_query: str = ""
    ) -> list[dict[str, Any]]:
        self.last_authored_extra_query = extra_query
        if self.raise_on_search is not None:
            raise self.raise_on_search
        if not self.auth_ok:
            raise AuthError("fake gh search: not logged in")
        if self.rate_limited:
            raise RateLimitError("fake gh: rate limited")
        if not username:
            raise PermanentError("fake gh: empty username")
        return self._search_items(self._authored_set)

    def _search_items(self, keys: set[tuple[str, int]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for repo, pr_number in keys:
            pr = self._prs.get((repo, pr_number))
            if pr is None:
                continue
            items.append(
                {
                    "number": pr_number,
                    "repository_url": f"https://api.github.com/repos/{repo}",
                    "pull_request": {
                        "url": f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
                        "draft": pr.draft,
                    },
                }
            )
        return items

    async def pr_get(self, repo: str, pr_number: int) -> dict[str, Any]:
        pr = self._prs.get((repo, pr_number))
        if pr is None:
            raise PermanentError(f"gh GET /repos/{repo}/pulls/{pr_number}: HTTP 404")
        return {
            "number": pr.pr_number,
            "title": pr.title,
            "body": pr.body,
            "head": {"sha": pr.head_sha},
            "user": {"login": pr.author},
            "draft": pr.draft,
            "state": pr.state,
            "requested_reviewers": [{"login": login} for login in pr.requested],
        }

    async def pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        pr = self._prs.get((repo, pr_number))
        if pr is None:
            raise PermanentError(f"gh GET pulls/{pr_number}/files: HTTP 404")
        return [dict(f) for f in pr.files]

    async def post_review(
        self,
        repo: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
        event: str = "COMMENT",
        login: str | None = None,
    ) -> dict[str, Any]:
        del login  # the real wrapper uses this for 5xx dedup; fake never 5xx-loops
        if self.raise_on_post is not None:
            raise self.raise_on_post
        if not self.auth_ok:
            raise AuthError("fake gh post: not logged in")
        if self.rate_limited:
            raise TransientError("fake gh: 503 service unavailable")
        self._next_review_id += 1
        review_id = self.post_review_response_id + self._next_review_id - 1
        record = {
            "repo": repo,
            "pr_number": pr_number,
            "commit_id": commit_id,
            "body": body,
            "comments": list(comments),
            "event": event,
            "review_id": review_id,
        }
        self._posted_reviews.append(record)
        gh_state = "APPROVED" if event == "APPROVE" else "COMMENTED"
        return {
            "id": review_id,
            "submitted_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "state": gh_state,
            "html_url": f"https://github.com/{repo}/pull/{pr_number}#pullrequestreview-{review_id}",
        }

    async def list_prior_reviews_with_comments(
        self,
        repo: str,
        pr_number: int,
        *,
        login: str,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Return seeded prior reviews — tests preload via `seed_prior_reviews(...)`."""
        del repo, pr_number, login
        return list(self._prior_reviews[:limit])

    def seed_prior_reviews(self, reviews: list[dict[str, Any]]) -> None:
        """Test seam — preload the prior-reviews list for the next handler call."""
        self._prior_reviews = list(reviews)


__all__ = ["FakeGh"]
