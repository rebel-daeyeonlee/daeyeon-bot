"""Curated LGTM GIFs embedded on APPROVE reviews (operator house style).

When the `pr_review` handler posts a GitHub **APPROVE** event, it drops a
celebratory GIF into the Summary — the operator's lightweight "LGTM" signal.

The GIF list is a *static curated set* harvested from
https://www.lgtmgifs.com/ (a Giphy-backed gallery — no public random-GIF
API). We deliberately do NOT fetch the site at runtime: the 24/7 daemon takes
no new network dependency, there's no HTML to parse on every approval, and the
behavior can't break when the gallery's markup changes. The trade-off is that
refreshing the roster is a code change — re-harvest URLs and edit `_LGTM_GIFS`.

Each entry is `(slug, giphy_media_id)`. The canonical embeddable URL is
`https://media.giphy.com/media/<id>/giphy.gif` — the animated full-size form,
which GitHub renders inline (via its camo image proxy). We store only the
stable media id, not the `media*.giphy.com/.../200w.webp` URL the gallery
serves, because that carries a `cid=` analytics token in the path that may rot.

The pick is deterministic per head SHA so a force re-review of the same commit
shows the same GIF (no churn in the rendered review).
"""

from __future__ import annotations

# (slug, giphy media id) — harvested 2026-06-02 from lgtmgifs.com.
_LGTM_GIFS: tuple[tuple[str, str], ...] = (
    ("happy-colbert", "WUq1cg9K7uzHa"),
    ("michael-scott-thank-you", "n4oKYFlAcv2AU"),
    ("dwight-thumb", "AAtjPSxgpO4AqpnA12"),
    ("kermit-ship-it", "KDorkt9e3T617FUxLz"),
    ("yoda-very-good", "3ohuAnWilO3JcRtCMw"),
    ("rocket-thumb", "Khl6ohcDKErosSbs9M"),
    ("leo-clap", "gLu90OMjz4j3hAkJk2"),
    ("well-done-minions", "fxsqOYnIMEefC"),
    ("one-punch-man", "bSGXw1QUjoWZ4YVFKz"),
    ("yes-yes-yes", "dYZuqJLDVsWMLWyIxJ"),
    ("cookie-monster-lgtm", "BNIzysgbLQZEf73HG0"),
    ("barney-i-like-it", "l2Je6zwsmFMhUxjoc"),
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


def pick_lgtm_gif(seed: str) -> str:
    """Return a markdown image line for an LGTM GIF, chosen by `seed` (head SHA)."""
    slug, gif_id = _LGTM_GIFS[_seed_index(seed, len(_LGTM_GIFS))]
    url = f"https://media.giphy.com/media/{gif_id}/giphy.gif"
    return f"![LGTM: {slug}]({url})"


__all__ = ["pick_lgtm_gif"]
