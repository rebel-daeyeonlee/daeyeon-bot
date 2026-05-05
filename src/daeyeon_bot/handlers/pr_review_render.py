"""Rendering helpers for the `pr_review` handler.

Two pure functions that don't depend on handler state:

- `render_user_message` — builds the diff snapshot prompt the way
  `contracts/claude-review-output.md` §2 specifies.
- `inline_to_api` — converts a validated `InlineComment` into the
  GitHub Reviews API payload (single-line vs multi-line anchor).

Split out of `pr_review.py` to keep that file under the 800-line soft
limit and to isolate prompt-shape changes from handler control flow.
"""

from __future__ import annotations

from typing import Any

from daeyeon_bot.handlers.pr_review_schemas import InlineComment


def inline_to_api(comment: InlineComment) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": comment.path,
        "line": comment.line,
        "side": comment.side,
        "body": comment.body,
    }
    if comment.start_line is not None:
        payload["start_line"] = comment.start_line
        payload["start_side"] = comment.side
    return payload


def render_user_message(
    *,
    repo: str,
    pr_number: int,
    title: str,
    body: str,
    author_login: str,
    head_sha: str,
    files: list[dict[str, Any]],
) -> str:
    """Render the snapshot the way `contracts/claude-review-output.md` §2 specs."""
    additions = sum(int(f.get("additions") or 0) for f in files)
    deletions = sum(int(f.get("deletions") or 0) for f in files)
    parts: list[str] = [
        f"Repository: {repo}",
        f"PR #{pr_number}: {title}",
        f"Author: @{author_login}",
        f"Head commit SHA: {head_sha}",
        "",
        "PR description:",
        "---",
        body,
        "---",
        "",
        f"Changed files ({len(files)}, +{additions} / -{deletions} lines):",
        "",
    ]
    for f in files:
        path = f.get("filename")
        status = f.get("status")
        adds = f.get("additions")
        dels = f.get("deletions")
        parts.append(f"### {path}  (status: {status}, +{adds}/-{dels})")
        patch = f.get("patch")
        if isinstance(patch, str):
            parts.append("```diff")
            parts.append(patch)
            parts.append("```")
        else:
            parts.append("(binary or oversized — diff omitted)")
        parts.append("")
    return "\n".join(parts)


__all__ = ["inline_to_api", "render_user_message"]
