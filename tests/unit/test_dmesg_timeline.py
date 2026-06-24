"""ssw-debugger dmesg-timeline bridge (feature 003)."""

from __future__ import annotations

from pathlib import Path

from daeyeon_bot.infra.dmesg_timeline import classify

# A stub standing in for ssw-debugger's dmesg-timeline.py: echoes a fixed
# `--json` payload shaped like the real script's output. Keeps the test
# hermetic (no dependency on the external plugin repo).
_STUB = """\
import json, sys
sys.stdin.read()
print(json.dumps({
    "events": [{"domain": "FW", "message": "rbln-fwi abort cl0", "severity": "error"}],
    "summary": {
        "by_domain": {"FW": 12, "KMD": 3},
        "by_severity": {"error": 10, "warn": 5},
    },
}))
"""


def _write_stub(tmp_path: Path, body: str = _STUB) -> str:
    p = tmp_path / "dmesg-timeline.py"
    p.write_text(body)
    return str(p)


async def test_classify_parses_summary(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    out = await classify("Jun 8 kernel: [rbln-fwi] abort", script_path=script)
    assert out is not None
    assert out.by_domain == {"FW": 12, "KMD": 3}
    assert out.earliest_message == "rbln-fwi abort cl0"
    prompt = out.as_prompt()
    assert "FW=12" in prompt and "owner_area" in prompt


async def test_empty_input_skips(tmp_path: Path) -> None:
    assert await classify("   ", script_path=_write_stub(tmp_path)) is None


async def test_no_script_path_skips() -> None:
    assert await classify("some log", script_path="") is None


async def test_missing_script_file_skips(tmp_path: Path) -> None:
    assert await classify("some log", script_path=str(tmp_path / "nope.py")) is None


async def test_nonzero_exit_skips(tmp_path: Path) -> None:
    script = _write_stub(tmp_path, "import sys; sys.exit(3)")
    assert await classify("some log", script_path=script) is None


async def test_unparsable_output_skips(tmp_path: Path) -> None:
    script = _write_stub(tmp_path, "print('not json')")
    assert await classify("some log", script_path=script) is None


async def test_summary_without_counts_skips(tmp_path: Path) -> None:
    # `{"summary": {}}` → no by_domain / by_severity → None
    script = _write_stub(
        tmp_path, "import json,sys; sys.stdin.read(); print(json.dumps({'summary': {}}))"
    )
    assert await classify("some log", script_path=script) is None
