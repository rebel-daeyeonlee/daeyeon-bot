"""Curated 곽철이 celebration GIFs embedded on a clean-pass review.

When the `pr_review` handler's verdict is `APPROVE` (zero findings), it drops a
곽철이 celebration GIF into the Summary — the operator's lightweight "가보자고 /
LGTM" signal. The bot never submits a GitHub APPROVE *event* (operator policy:
every review posts as a COMMENT), so this GIF is what marks a clean pass in the
rendered review.

The roster is a *static curated set* of 곽철이 mascot GIFs (Tenor-hosted, a
public CDN — GitHub renders them inline via its camo image proxy). We
deliberately do NOT fetch anything at runtime: the 24/7 daemon takes no new
network dependency, there's no HTML to parse on every approval, and the
behavior can't break when a gallery's markup changes. The trade-off is that
refreshing the roster is a code change — add a `(slug, url)` row.

Each entry is `(slug, url)` where `url` points directly at an animated `.gif`
(not a Tenor *page* URL — those don't render as images). The pick is
deterministic per head SHA so a force re-review of the same commit shows the
same GIF (no churn in the rendered review).
"""

from __future__ import annotations

# (slug, direct .gif url) — 곽철이 mascot GIFs, Tenor CDN. `url` MUST resolve to
# an image/gif with a public 200 (verified before adding). The %-escapes are the
# Korean filename encoded — leave them as-is; GitHub fetches the literal URL.
_CELEBRATE_GIFS: tuple[tuple[str, str], ...] = (
    (
        "가보자고",
        "https://media1.tenor.com/m/SSkFV5jQ6Q4AAAAd/%EA%B3%BD%EC%B2%A0%EC%9D%B4-%EA%B0%80%EB%B3%B4%EC%9E%90%EA%B3%A0.gif",
    ),
)


def _seed_index(seed: str, n: int) -> int:
    """Map `seed` (a head SHA) to an index in `[0, n)`, deterministically.

    Uses the leading hex of the SHA so the same commit always yields the same
    GIF. Falls back to a char-sum when `seed` isn't hex (defensive — head_sha
    is always hex in practice).
    """
    if n <= 0:
        return 0
    prefix = seed[:8] or "0"
    try:
        return int(prefix, 16) % n
    except ValueError:
        return sum(map(ord, seed)) % n


def pick_celebrate_gif(seed: str) -> str:
    """Return a markdown image line for a 곽철이 GIF, chosen by `seed` (head SHA)."""
    slug, url = _CELEBRATE_GIFS[_seed_index(seed, len(_CELEBRATE_GIFS))]
    return f"![곽철이 {slug}]({url})"


__all__ = ["pick_celebrate_gif"]
