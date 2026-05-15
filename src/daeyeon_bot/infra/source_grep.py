"""Evidence-driven source-code grep for the triage pipeline.

The handler extracts distinctive identifiers (snake_case symbol names,
`rbln*` camelCase APIs, `RBLN_*` log macro tags) from the ticket error
log + Loki slices, then greps the checked-out `ssw-bundle/products/`
tree for those identifiers. Hits become `ProductCodeFile` excerpts —
±10 lines of context around the match — which the handler passes to
Claude in the user message and into the `{code:title=product_code...}`
attachment block.

Pure functions where possible (`extract_tokens`); the actual grep is
an async subprocess call so a few-thousand-file scan doesn't block
the dispatcher event loop.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from daeyeon_bot.core.jira_triage.types import ProductCodeFile

_MAX_TOKENS = 8
_MAX_EXCERPTS = 5
_EXCERPT_PADDING = 10
_GREP_TIMEOUT_S = 30

# camelCase `rblnSomething` ≥ 8 chars total (filters out very short names
# that produce too many false-positive hits like `rbln`, `rblnE`).
_CAMEL_RE = re.compile(r"\brbln[A-Z][A-Za-z][A-Za-z0-9]{4,}\b")

# snake_case with ≥ 2 underscores (e.g. `rbln_retracer_process_func_call`).
# Stricter than allowing 1 underscore — single-underscore names like
# `host_ip` or `test_code` aren't useful grep terms.
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9_]*_[a-z0-9_]+_[a-z0-9_]+[a-z0-9]\b")

# RBLN macro tag inside square brackets, e.g. `[RBLN_TRACE_retracer_special_ERR]`.
_MACRO_TAG_RE = re.compile(r"\[(RBLN_[A-Za-z][A-Za-z0-9_]*)\]")

# Words that look like identifiers but are too generic to find anything
# distinctive in source code (would just match boilerplate everywhere).
_NOISE_TOKENS: frozenset[str] = frozenset(
    {
        "error_log",
        "test_code",
        "product_code",
        "ticket_error_log",
        "regression_test",
        "fail_dev_ids",
        "infer_cnt",
        "iter_cnt",
        "elapsed_time_us",
        "func_call",
        "max_bus_speed",
        "max_bus_width",
        "boot_done",
        "reset_cnt",
        "hw_status",
        "callno",
        "func",
        "Documentation",
    }
)


def extract_tokens(texts: list[str], *, max_tokens: int = _MAX_TOKENS) -> list[str]:
    """Pull distinctive identifiers from log/error text for source-code lookup.

    Strategy:
      - `RBLN_*` macro tags (highest signal — log identifies the layer)
      - `rblnCamelCase` APIs (UMD/SDK public surface)
      - `snake_case_with_3_parts` (function names)
      - Drop common noise tokens
      - Sort by (specificity → frequency) descending
      - Return at most `max_tokens`
    """
    seen: dict[str, int] = {}
    for text in texts:
        if not text:
            continue
        for m in _MACRO_TAG_RE.finditer(text):
            tok = m.group(1)
            if tok not in _NOISE_TOKENS:
                seen[tok] = seen.get(tok, 0) + 1
        for m in _CAMEL_RE.finditer(text):
            tok = m.group(0)
            if tok not in _NOISE_TOKENS:
                seen[tok] = seen.get(tok, 0) + 1
        for m in _SNAKE_RE.finditer(text):
            tok = m.group(0)
            if tok in _NOISE_TOKENS:
                continue
            # Reject tokens that contain only one segment of letters with
            # numeric padding — almost always noise (e.g. ip-octet patterns).
            if any(p.isdigit() for p in tok.split("_")):
                continue
            seen[tok] = seen.get(tok, 0) + 1

    # Specificity proxy: longer token + macro/RBLN prefix > shorter + plain.
    def _key(item: tuple[str, int]) -> tuple[int, int, int]:
        tok, freq = item
        rbln_bonus = 1 if tok.startswith(("RBLN_", "rbln")) else 0
        return (-rbln_bonus, -len(tok), -freq)

    return [tok for tok, _ in sorted(seen.items(), key=_key)[:max_tokens]]


async def grep_excerpts(
    *,
    bundle_path: Path,
    tokens: list[str],
    max_excerpts: int = _MAX_EXCERPTS,
    padding: int = _EXCERPT_PADDING,
) -> tuple[ProductCodeFile, ...]:
    """Grep `<bundle>/products/` for each token; return windowed excerpts.

    Uses async subprocess `grep -rnE` over a fixed extension allowlist
    so a recursive walk into `node_modules` / binary blobs is impossible.
    Bounded by `_GREP_TIMEOUT_S` — on timeout returns whatever's parsed.
    Each excerpt: ±`padding` lines around the match line, with the
    hit line marked by `>` in the gutter.
    """
    if not tokens:
        return ()
    products = bundle_path / "products"
    if not products.exists():
        return ()

    pattern = "|".join(re.escape(t) for t in tokens)
    # `--color=never` is portable across GNU/BSD grep; older GNU grep
    # rejects `--no-color`. Stdout is piped so colors would be off by
    # default anyway, but be explicit for any grep alias that forces them.
    cmd = [
        "grep",
        "-rnE",
        "--color=never",
        "--include=*.c",
        "--include=*.cpp",
        "--include=*.cc",
        "--include=*.h",
        "--include=*.hpp",
        "--include=*.py",
        "--include=*.rs",
        "--include=*.go",
        pattern,
        str(products),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return ()

    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=_GREP_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ()

    # grep returns 1 when there are no matches — that's not an error here.
    if proc.returncode not in (0, 1):
        return ()

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    return _parse_grep_hits(
        stdout=stdout,
        bundle_path=bundle_path,
        max_excerpts=max_excerpts,
        padding=padding,
    )


def _parse_grep_hits(
    *,
    stdout: str,
    bundle_path: Path,
    max_excerpts: int,
    padding: int,
) -> tuple[ProductCodeFile, ...]:
    """Parse `grep -rn` output into `ProductCodeFile` excerpts (file-deduped)."""
    excerpts: list[ProductCodeFile] = []
    seen_files: set[str] = set()
    for raw_line in stdout.splitlines():
        if len(excerpts) >= max_excerpts:
            break
        # `grep -n` output: `<absolute path>:<line>:<content>`
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        path_str, line_no_str, _content = parts
        try:
            line_no = int(line_no_str)
        except ValueError:
            continue
        try:
            rel = Path(path_str).resolve().relative_to(bundle_path.resolve())
        except ValueError:
            continue
        relpath = str(rel)
        if relpath in seen_files:
            continue
        excerpt_text = _read_excerpt(bundle_path / relpath, line_no, padding)
        if excerpt_text is None:
            continue
        excerpts.append(
            ProductCodeFile(
                submodule_path=_submodule_root(relpath),
                file_path=relpath,
                excerpt=excerpt_text,
            )
        )
        seen_files.add(relpath)
    return tuple(excerpts)


def _read_excerpt(path: Path, line_no: int, padding: int) -> str | None:
    """±padding lines around `line_no` (1-based), with `>` marker on the hit."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.split("\n")
    start = max(0, line_no - 1 - padding)
    end = min(len(lines), line_no + padding)
    out: list[str] = []
    for i in range(start, end):
        marker = " >" if (i + 1) == line_no else "  "
        out.append(f"{i + 1:>5}{marker} {lines[i]}")
    return "\n".join(out)


def _submodule_root(relpath: str) -> str:
    """Heuristic — `products/<sw>/<sub>/...` → `products/<sw>/<sub>`."""
    parts = relpath.split("/")
    if len(parts) >= 3 and parts[0] == "products":
        return "/".join(parts[:3])
    return "products"


__all__ = [
    "extract_tokens",
    "grep_excerpts",
]
