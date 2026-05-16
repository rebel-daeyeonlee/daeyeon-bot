"""Pure builders for `daeyeon-bot dev fire-*` event payloads.

Extracted out of `cli/dev.py` so we can unit-test the payload + dedup
key shape without spawning subprocesses or hitting the `gh` / `jira`
clients. The CLI commands stay thin wrappers — fetch metadata, call
these builders, write to the outbox.

`build_pr_review_payload` and `build_jira_triage_payload` return a
`(payload, dedup_key)` tuple. The dedup key is a SHA-256 over the same
fields the auto-triggers use, so a manual fire collides correctly with
an in-flight auto event at the same identity.
"""

from __future__ import annotations

import hashlib
import time


def build_pr_review_payload(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    force: bool,
) -> tuple[dict[str, object], str]:
    """Build the `pr.review.manual` payload + dedup key.

    `request_gen` is INT per the handler schema. Force fires bump the
    generation with `int(time.time())` so the audit dedup row doesn't
    collide with the prior gen=0 auto-trigger row at the same SHA.
    """
    request_gen = int(time.time()) if force else 0
    payload: dict[str, object] = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "request_gen": request_gen,
        "force": force,
    }
    dedup_seed = f"manual-pr-review|{repo}#{pr_number}@{head_sha}|{request_gen}|{force}"
    dedup_key = hashlib.sha256(dedup_seed.encode("utf-8")).hexdigest()
    return payload, dedup_key


def build_jira_triage_payload(
    *,
    issue_key: str,
    force: bool,
) -> tuple[dict[str, object], str]:
    """Build the `jira.triage.manual` payload + dedup key.

    `comment_seq` keys the audit-row dedup: a non-force re-fire collides
    with the existing `comment_seq="1"` row and short-circuits; a force
    fire bumps `comment_seq` to `manual_<unix_ts>` so the handler treats
    it as a distinct re-triage and prepends a supersede header on the
    new comment.
    """
    comment_seq = f"manual_{int(time.time())}" if force else "1"
    payload: dict[str, object] = {
        "issue_key": issue_key,
        "force": force,
        "comment_seq": comment_seq,
    }
    dedup_seed = f"manual-jira-triage|{issue_key}|{comment_seq}"
    dedup_key = hashlib.sha256(dedup_seed.encode("utf-8")).hexdigest()
    return payload, dedup_key


__all__ = [
    "build_jira_triage_payload",
    "build_pr_review_payload",
]
