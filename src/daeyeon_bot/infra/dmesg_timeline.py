"""Optional bridge to ssw-debugger's `dmesg-timeline.py` classifier (feature 003).

ssw-debugger (oh-my-debugger) ships a stdlib-only script that parses kernel /
syslog lines into a domain-classified timeline (`[rbln-fwi]`→FW, `[rbln-rbl]`→
KMD, `[rbln-mctp]`→SMC, …) with severity + time-gap tagging. We pipe the device
logs the handler already fetched from Loki through it to get a domain
distribution that sharpens `owner_area` — exactly the "attach the debugger"
enrichment, but as a deterministic subprocess (no Claude subagents, no MCP).

Fully optional and best-effort: no `[handlers.ci_triage].dmesg_timeline_script`
configured, script missing, non-stdlib failure, timeout, or unparsable output →
`classify` returns None and triage proceeds unchanged. The script is treated as
untrusted: only its PARSED summary reaches the prompt (the caller still redacts
it), never raw stdout.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, cast

_DEFAULT_TIMEOUT = 20.0
# Domain → daeyeon-bot owner_area hint (ssw-debugger uses FW/CpFw split; the
# handler's persona maps these). Kept here only for the prompt hint string.
_DOMAIN_HINT = "FW→SysFw/CpFw, KMD→Driver, SMC→HW/SysFw, Tools→DevOps, RF→test"


@dataclass(frozen=True, slots=True)
class DmesgSummary:
    """The distilled signal from one dmesg-timeline run."""

    by_domain: dict[str, int]
    by_severity: dict[str, int]
    earliest_message: str | None

    def as_prompt(self) -> str:
        """Compact evidence block for the Claude prompt."""
        dom = ", ".join(
            f"{k}={v}" for k, v in sorted(self.by_domain.items(), key=lambda kv: -kv[1])
        )
        sev = ", ".join(
            f"{k}={v}" for k, v in sorted(self.by_severity.items(), key=lambda kv: -kv[1])
        )
        lines = [
            "## Device-log domain timeline (ssw-debugger dmesg-timeline.py)",
            f"- lines by domain: {dom or '(none)'}",
            f"- by severity: {sev or '(none)'}",
        ]
        if self.earliest_message:
            lines.append(f"- earliest classified line: {self.earliest_message}")
        lines.append(
            f"Use the dominant error domain to sharpen owner_area ({_DOMAIN_HINT}). "
            "This is a heuristic tag over the Loki slice, not ground truth."
        )
        return "\n".join(lines)


async def classify(
    device_log: str,
    *,
    script_path: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT,
) -> DmesgSummary | None:
    """Pipe `device_log` (Loki kernel slice) through dmesg-timeline.py --json.

    Returns the parsed summary, or None on any condition that means "no
    enrichment" (empty input, no/missing script, subprocess error, timeout,
    non-zero exit, unparsable JSON). A missing script makes the subprocess exit
    non-zero, which folds into the returncode guard. Never raises."""
    if not device_log.strip() or not script_path:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            script_path,
            "--json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(device_log.encode("utf-8")), timeout=timeout_seconds
        )
    except (FileNotFoundError, OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_summary(stdout)


def _parse_summary(stdout: bytes) -> DmesgSummary | None:
    """Parse dmesg-timeline `--json` output (untrusted) into a DmesgSummary."""
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    d = cast("dict[str, Any]", data)
    summary = d.get("summary")
    if not isinstance(summary, dict):
        return None
    s = cast("dict[str, Any]", summary)
    by_domain = _int_map(s.get("by_domain"))
    by_severity = _int_map(s.get("by_severity"))
    if not by_domain and not by_severity:
        return None
    return DmesgSummary(
        by_domain=by_domain,
        by_severity=by_severity,
        earliest_message=_earliest(d.get("events")),
    )


def _int_map(value: object) -> dict[str, int]:
    """Coerce a {str: int} mapping from untrusted JSON, dropping bad entries."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in cast("dict[Any, Any]", value).items():
        if isinstance(k, str) and isinstance(v, int):
            out[k] = v
    return out


def _earliest(events: object) -> str | None:
    """First event's message (truncated), or None."""
    if not isinstance(events, list) or not events:
        return None
    first = events[0]
    if not isinstance(first, dict):
        return None
    msg = first.get("message")
    if not isinstance(msg, str) or not msg.strip():
        return None
    return msg.strip()[:160]


__all__ = ["DmesgSummary", "classify"]
