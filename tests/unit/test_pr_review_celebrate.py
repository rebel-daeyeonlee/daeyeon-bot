"""Tests for the 곽철이 celebration-GIF picker (`handlers/pr_review_celebrate.py`)."""

from __future__ import annotations

from daeyeon_bot.handlers.pr_review_celebrate import (
    _CELEBRATE_GIFS,  # pyright: ignore[reportPrivateUsage]
    _seed_index,  # pyright: ignore[reportPrivateUsage]
    pick_celebrate_gif,
)


def test_pick_returns_markdown_image_line() -> None:
    out = pick_celebrate_gif("deadbeef")
    assert out.startswith("![곽철이 ")
    assert "](http" in out
    assert out.endswith(".gif)")


def test_pick_is_deterministic_per_seed() -> None:
    """A force re-review of the same commit must render the same GIF."""
    assert pick_celebrate_gif("deadbeef") == pick_celebrate_gif("deadbeef")


def test_seed_index_within_bounds_for_hex() -> None:
    n = len(_CELEBRATE_GIFS)
    for sha in ("0", "ffffffff", "abc123de", "00000007"):
        assert 0 <= _seed_index(sha, n) < n


def test_seed_index_tolerates_non_hex() -> None:
    n = len(_CELEBRATE_GIFS)
    assert 0 <= _seed_index("not-hex-zzz", n) < n


def test_seed_index_empty_list() -> None:
    assert _seed_index("deadbeef", 0) == 0


def test_all_gifs_are_direct_gif_urls() -> None:
    assert _CELEBRATE_GIFS, "roster must not be empty"
    for slug, url in _CELEBRATE_GIFS:
        assert slug and url
        assert url.startswith("https://")
        assert url.endswith(".gif")
        assert " " not in url
