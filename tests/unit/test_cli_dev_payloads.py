"""Unit tests for `cli/_dev_payloads.py` — `dev fire-*` payload builders.

Covers the bits the broader `cli/dev.py` happy-path tests can't easily
reach: int request_gen for force fires, comment_seq sentinels, dedup
key stability for the non-force path.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from daeyeon_bot.cli._dev_payloads import (
    build_ci_triage_payload,
    build_jira_triage_payload,
    build_pr_review_payload,
)


def _freeze_time(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    """Pin `time.time()` to a deterministic int so dedup keys are reproducible."""

    def _fake() -> float:
        return float(value)

    monkeypatch.setattr(time, "time", _fake)


# ── pr_review payload ────────────────────────────────────────────────────────


def test_pr_review_non_force_uses_zero_request_gen(monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze_time(monkeypatch, 1_700_000_000)
    payload, _ = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="deadbeef", force=False)
    assert payload == {
        "repo": "o/r",
        "pr_number": 7,
        "head_sha": "deadbeef",
        "request_gen": 0,
        "force": False,
    }


def test_pr_review_force_bumps_request_gen_to_int_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force fires use `int(time.time())` so dedup doesn't collide with auto gen=0."""
    _freeze_time(monkeypatch, 1_700_000_000)
    payload, _ = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="deadbeef", force=True)
    assert payload["force"] is True
    assert isinstance(payload["request_gen"], int)
    assert payload["request_gen"] == 1_700_000_000


def test_pr_review_request_gen_is_int_not_str_regression() -> None:
    """The handler validator rejects string `request_gen` — see f217c23."""
    payload, _ = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="deadbeef", force=True)
    assert isinstance(payload["request_gen"], int), (
        "request_gen must be int — handler will dead-letter on ValidationError"
    )


def test_pr_review_dedup_key_is_stable_for_non_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-force fires use deterministic dedup so re-running collides with prior."""
    _, key1 = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="abc", force=False)
    _, key2 = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="abc", force=False)
    assert key1 == key2


def test_pr_review_dedup_key_varies_by_force_flag() -> None:
    """A force fire must NOT collide with the prior non-force entry."""
    _, key_off = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="abc", force=False)
    _, key_on = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="abc", force=True)
    assert key_off != key_on


def test_pr_review_dedup_key_varies_by_head_sha() -> None:
    _, k1 = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="abc", force=False)
    _, k2 = build_pr_review_payload(repo="o/r", pr_number=7, head_sha="xyz", force=False)
    assert k1 != k2


# ── jira_triage payload ──────────────────────────────────────────────────────


def test_jira_non_force_uses_comment_seq_one() -> None:
    payload, _ = build_jira_triage_payload(issue_key="SSWCI-100", force=False)
    assert payload == {
        "issue_key": "SSWCI-100",
        "force": False,
        "comment_seq": "1",
    }


def test_jira_force_bumps_comment_seq_to_manual_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_time(monkeypatch, 1_700_000_000)
    payload, _ = build_jira_triage_payload(issue_key="SSWCI-100", force=True)
    assert payload["force"] is True
    seq: Any = payload["comment_seq"]
    assert isinstance(seq, str)
    assert seq.startswith("manual_")
    assert seq.endswith("1700000000")


def test_jira_dedup_key_is_stable_for_non_force() -> None:
    _, k1 = build_jira_triage_payload(issue_key="SSWCI-100", force=False)
    _, k2 = build_jira_triage_payload(issue_key="SSWCI-100", force=False)
    assert k1 == k2


def test_jira_dedup_key_varies_per_force_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two force fires at different times must produce different dedup keys."""
    _freeze_time(monkeypatch, 1_700_000_000)
    _, k1 = build_jira_triage_payload(issue_key="SSWCI-100", force=True)
    _freeze_time(monkeypatch, 1_700_000_001)
    _, k2 = build_jira_triage_payload(issue_key="SSWCI-100", force=True)
    assert k1 != k2


def test_jira_dedup_key_varies_by_issue_key() -> None:
    _, k1 = build_jira_triage_payload(issue_key="SSWCI-100", force=False)
    _, k2 = build_jira_triage_payload(issue_key="SSWCI-200", force=False)
    assert k1 != k2


def test_ci_triage_omits_thread_coords_by_default() -> None:
    payload, _ = build_ci_triage_payload(
        repo="rebellions-sw/ssw-bundle", run_id="123", force=False
    )
    assert payload == {"repo": "rebellions-sw/ssw-bundle", "run_id": "123", "force": False}


def test_ci_triage_carries_thread_coords_when_both_given() -> None:
    payload, _ = build_ci_triage_payload(
        repo="rebellions-sw/ssw-bundle",
        run_id="123",
        force=False,
        channel_id="C09SEN8MH5M",
        message_ts="1781842790.331459",
    )
    assert payload["channel_id"] == "C09SEN8MH5M"
    assert payload["message_ts"] == "1781842790.331459"


def test_ci_triage_drops_thread_coords_when_partial() -> None:
    """Only one of channel_id/message_ts → neither is carried (incomplete thread)."""
    payload, _ = build_ci_triage_payload(
        repo="rebellions-sw/ssw-bundle", run_id="123", force=False, channel_id="C09SEN8MH5M"
    )
    assert "channel_id" not in payload
    assert "message_ts" not in payload


def test_ci_triage_thread_coords_do_not_change_dedup_key() -> None:
    """Dedup identity is (repo, run_id) — routing coords must not split it."""
    _, k_plain = build_ci_triage_payload(
        repo="rebellions-sw/ssw-bundle", run_id="123", force=False
    )
    _, k_thread = build_ci_triage_payload(
        repo="rebellions-sw/ssw-bundle",
        run_id="123",
        force=False,
        channel_id="C09SEN8MH5M",
        message_ts="1781842790.331459",
    )
    assert k_plain == k_thread
