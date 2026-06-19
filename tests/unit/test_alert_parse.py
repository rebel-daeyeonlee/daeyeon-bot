"""Alert parsing against the 3 real alert shapes (verified live 2026-06-19)."""

from __future__ import annotations

from daeyeon_bot.infra.alert_parse import (
    extract_run_ref,
    is_ci_failure_candidate,
    merge_message_text,
    parse_alert,
)

_KNOWN_BOTS = frozenset({"U069J27G2G6", "U09RJGLLPLZ"})

# sukju-bot in #help — top-level text, structured bullets.
_SUKJU = {
    "user": "USUKJU",
    "ts": "1781674470.655379",
    "text": (
        "`ASIC CI 지속 실패 — CR13`\n"
        "• PR: https://github.com/rebellions-sw/ssw-common-kmd/pull/1719 (#1719)\n"
        "• head SHA: 656ecb05\n"
        "• 실패 job: premerge / rebel-CR13-premerge / CR13-premerge-phase0\n"
        "• 실패 run: https://github.com/rebellions-sw/ssw-common-kmd/actions/runs/27659850320/job/81821665219?pr=1719\n"
        "• 연속 실패: 2회 (자동 rerun에도 미해결)\n"
        "> :robot_face: sukju-bot"
    ),
}

# dev_syssw_test in #alerts — top-level mrkdwn, [host] tag + Workflow link.
_DEVSYSSW = {
    "user": "U069J27G2G6",
    "ts": "1781750772.973099",
    "text": (
        ":warning: *[ssw-smci-16] Premerge 디바이스 복구 실패*\n"
        "• *Workflow:* <https://github.com/rebellions-sw/ssw-bundle/actions/runs/27725792803|Run Link>\n"
        "• *Phase:* phase1\n"
        "• *Runner:* `ssw-rebel-vm-02-runner`\n"
        "• *Logs:* <https://grafana.ssw.rbln.in/explore?panes=%7B%22datasource%22%3A%22loki%22%2C%22queries%22%3A%5B%7B%22expr%22%3A%22%7Bhostname%3D~%5C%22ssw-smci-16%5C%22%7D%22%7D%5D%2C%22range%22%3A%7B%22from%22%3A%221781748972742%22%2C%22to%22%3A%221781750772742%22%7D%7D|syslog>"
    ),
}

# SSW-Alert-Bot in #alerts — Grafana alerting, content ONLY in attachments[].text.
_SSW_ALERT = {
    "user": "U09RJGLLPLZ",
    "ts": "1781795270.581049",
    "text": "",
    "attachments": [
        {
            "color": "D63232",
            "title": "CIHealthcheckJobFailed - firing",
            "title_link": "https://grafana.ssw.rbln.in/alerting/grafana/ci-healthcheck-job-failed/view",
            "text": (
                "*:rotating_light: Alerts — 총 1건*\n"
                ":fire: *:red_circle: CRITICAL* • *CIHealthcheckJobFailed*\n"
                "• :memo: *Summary:* Healthcheck job CR03-premerge-phase2 failed on dev — "
                "<https://github.com/rebellions-sw/ssw-bundle/actions/runs/27758520154/job/82144094445>"
            ),
            "fallback": "CIHealthcheckJobFailed - firing",
        }
    ],
}


def test_sukju_run_ref_and_meta() -> None:
    parsed = parse_alert(_SUKJU, channel_id="C_HELP")
    assert parsed.run_ref is not None
    assert parsed.run_ref.repo == "rebellions-sw/ssw-common-kmd"
    assert parsed.run_ref.run_id == "27659850320"
    assert parsed.head_sha == "656ecb05"
    assert parsed.consecutive_fail_count == 2
    assert parsed.failed_jobs == ("premerge / rebel-CR13-premerge / CR13-premerge-phase0",)
    assert parsed.pr_number == 1719


def test_devsysswtest_run_ref_and_loki_window() -> None:
    parsed = parse_alert(_DEVSYSSW, channel_id="C_ALERTS")
    assert parsed.run_ref is not None
    assert parsed.run_ref.repo == "rebellions-sw/ssw-bundle"
    assert parsed.run_ref.run_id == "27725792803"
    assert parsed.loki_window is not None
    assert parsed.loki_window.host == "ssw-smci-16"
    assert parsed.loki_window.start == "1781748972742"
    assert parsed.loki_window.end == "1781750772742"


def test_ssw_alert_bot_run_ref_from_attachments() -> None:
    """The run link lives ONLY in attachments[].text — top-level text is empty."""
    merged = merge_message_text(_SSW_ALERT)
    assert "CIHealthcheckJobFailed" in merged
    parsed = parse_alert(_SSW_ALERT, channel_id="C_ALERTS")
    assert parsed.run_ref is not None
    assert parsed.run_ref.repo == "rebellions-sw/ssw-bundle"
    assert parsed.run_ref.run_id == "27758520154"


def test_candidate_filter() -> None:
    # Known bot author → candidate even without a link.
    assert is_ci_failure_candidate(
        {"user": "U069J27G2G6", "text": "no link here"}, known_bot_ids=_KNOWN_BOTS
    )
    # Unknown author but has a run link → candidate.
    assert is_ci_failure_candidate(_SUKJU, known_bot_ids=_KNOWN_BOTS)
    # SSW-Alert-Bot: link is in attachments, author is known → candidate.
    assert is_ci_failure_candidate(_SSW_ALERT, known_bot_ids=_KNOWN_BOTS)
    # Pure human chatter, no link, unknown author → NOT a candidate.
    assert not is_ci_failure_candidate(
        {"user": "UHUMAN", "text": "approve 하니 auto merge 됐네요 ^^;;"},
        known_bot_ids=_KNOWN_BOTS,
    )


def test_no_run_link_returns_none() -> None:
    assert extract_run_ref("just some text with a https://github.com/foo/bar link") is None
