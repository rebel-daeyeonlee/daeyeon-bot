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


def build_ci_triage_payload(
    *,
    repo: str,
    run_id: str,
    force: bool,
    channel_id: str | None = None,
    message_ts: str | None = None,
) -> tuple[dict[str, object], str]:
    """Build the `ci.triage.manual` payload + dedup key (feature 003).

    Non-force fires dedup on `(repo, run_id)` so a re-fire of the same run
    collides with the in-flight event; a force fire appends the unix ts so it
    is treated as a distinct re-triage (a new audit row + supersede header).

    `channel_id` + `message_ts` are the source alert's thread coordinates. When
    both are given, the handler replies in that real thread under
    post_target="thread"; absent them it falls back to dry_run_channel.
    """
    payload: dict[str, object] = {"repo": repo, "run_id": run_id, "force": force}
    if channel_id and message_ts:
        payload["channel_id"] = channel_id
        payload["message_ts"] = message_ts
    dedup_seed = f"manual-ci-triage|{repo}|{run_id}"
    if force:
        dedup_seed = f"{dedup_seed}|{int(time.time())}"
    dedup_key = hashlib.sha256(dedup_seed.encode("utf-8")).hexdigest()
    return payload, dedup_key


__all__ = [
    "build_ci_triage_payload",
    "build_jira_triage_payload",
    "build_pr_review_payload",
]
