"""Source-grep token extraction + bundle search."""

from __future__ import annotations

from pathlib import Path

import pytest

from daeyeon_bot.infra.source_grep import extract_tokens, grep_excerpts

# ── extract_tokens ──────────────────────────────────────────────────────────


def test_extract_picks_rbln_macro_tags() -> None:
    text = (
        "[RBLN_TRACE_retracer_special_ERR] rbln_retracer_process_func_call: failed\n"
        "[RBLN_UMD_api_WARN] rblnDestroyContext: const_buf_pool not destroyed\n"
    )
    out = extract_tokens([text])
    assert "RBLN_TRACE_retracer_special_ERR" in out
    assert "RBLN_UMD_api_WARN" in out


def test_extract_picks_camelcase_rbln_apis() -> None:
    text = "rblnDestroyContext failed before rblnCreateBuffer.\n"
    out = extract_tokens([text])
    assert "rblnDestroyContext" in out
    assert "rblnCreateBuffer" in out


def test_extract_picks_snake_case_functions() -> None:
    text = "kernel: [rbln-rbl] rebel_hard_reset_for_topology: rc=-110"
    out = extract_tokens([text])
    assert "rebel_hard_reset_for_topology" in out


def test_extract_filters_short_camelcase() -> None:
    """`rblnA` / `rblnE` are too short and produce false-positive grep hits."""
    out = extract_tokens(["error in rblnA and rblnE"])
    assert "rblnA" not in out
    assert "rblnE" not in out


def test_extract_filters_noise_tokens() -> None:
    """Generic words like `error_log`, `test_code`, etc. should be dropped."""
    text = "error_log says test_code failed product_code missing"
    out = extract_tokens([text])
    assert "error_log" not in out
    assert "test_code" not in out
    assert "product_code" not in out


def test_extract_filters_digit_padded_tokens() -> None:
    """Single-segment + digit suffix like `addr_10c00000_hop` is too specific."""
    out = extract_tokens(["addr_10c00000_hop_2_idx_86"])
    # Pure-digit segments removed → token rejected.
    assert all("addr_10c00000" not in t for t in out)


def test_extract_caps_at_max_tokens() -> None:
    """Even with lots of matches, output is bounded."""
    suffixes = [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "iota",
        "kappa",
    ]
    text = " ".join(f"rbln_function_name_{s}_call" for s in suffixes)
    out = extract_tokens([text], max_tokens=5)
    assert len(out) == 5


def test_extract_prefers_rbln_prefix() -> None:
    """RBLN-prefixed tokens should outrank equally-long generic snake_case."""
    text = "some_generic_function_call_here and rbln_specific_helper_func"
    out = extract_tokens([text])
    # Both candidates qualify; rbln_ prefix wins.
    idx_rbln = out.index("rbln_specific_helper_func")
    idx_generic = out.index("some_generic_function_call_here")
    assert idx_rbln < idx_generic


def test_extract_skips_empty_input() -> None:
    assert extract_tokens([]) == []
    assert extract_tokens(["", "   "]) == []


# ── grep_excerpts ───────────────────────────────────────────────────────────


@pytest.fixture
def fake_bundle(tmp_path: Path) -> Path:
    """Minimal `products/` tree with a few source files to grep."""
    products = tmp_path / "products"
    (products / "common" / "umd" / "src").mkdir(parents=True)
    (products / "atom" / "fw" / "src").mkdir(parents=True)
    (products / "common" / "umd" / "src" / "api.c").write_text(
        "\n".join(
            [
                "// line 1",
                "// line 2",
                "// line 3",
                "void rblnDestroyContext(Context* ctx) {",
                "    if (ctx->const_buf_pool[0][0] != NULL) {",
                '        LOG_WARN("const_buf_pool not destroyed");',
                "    }",
                "    free(ctx);",
                "}",
                "",
                "// line 11",
            ]
        ),
        encoding="utf-8",
    )
    (products / "atom" / "fw" / "src" / "cmd_queue.c").write_text(
        "\n".join(
            [
                "void rbln_queue_reset(int qid) {",
                "    abort_handler(qid);",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    # A non-source file we should ignore.
    (products / "atom" / "fw" / "README.md").write_text(
        "rblnDestroyContext mention", encoding="utf-8"
    )
    return tmp_path


async def test_grep_finds_match_in_c_source(fake_bundle: Path) -> None:
    out = await grep_excerpts(bundle_path=fake_bundle, tokens=["rblnDestroyContext"])
    assert len(out) == 1
    hit = out[0]
    assert hit.file_path == "products/common/umd/src/api.c"
    assert hit.submodule_path == "products/common/umd"
    assert "rblnDestroyContext" in hit.excerpt
    assert " >" in hit.excerpt  # hit-line marker present


async def test_grep_skips_non_source_extensions(fake_bundle: Path) -> None:
    """`README.md` containing the token should NOT be matched."""
    out = await grep_excerpts(bundle_path=fake_bundle, tokens=["rblnDestroyContext"])
    paths = {h.file_path for h in out}
    assert all(not p.endswith(".md") for p in paths)


async def test_grep_returns_multiple_files(fake_bundle: Path) -> None:
    out = await grep_excerpts(
        bundle_path=fake_bundle,
        tokens=["rblnDestroyContext", "rbln_queue_reset"],
    )
    paths = {h.file_path for h in out}
    assert "products/common/umd/src/api.c" in paths
    assert "products/atom/fw/src/cmd_queue.c" in paths


async def test_grep_dedupes_by_file(fake_bundle: Path) -> None:
    """Same file matching multiple tokens → single excerpt."""
    # Both `const_buf_pool` and `rblnDestroyContext` live in the same file.
    out = await grep_excerpts(
        bundle_path=fake_bundle,
        tokens=["rblnDestroyContext", "const_buf_pool"],
    )
    api_hits = [h for h in out if h.file_path == "products/common/umd/src/api.c"]
    assert len(api_hits) == 1


async def test_grep_no_match_returns_empty(fake_bundle: Path) -> None:
    out = await grep_excerpts(
        bundle_path=fake_bundle, tokens=["this_symbol_does_not_exist_anywhere"]
    )
    assert out == ()


async def test_grep_missing_products_dir(tmp_path: Path) -> None:
    """No `products/` → empty result, no crash."""
    out = await grep_excerpts(bundle_path=tmp_path, tokens=["rblnDestroyContext"])
    assert out == ()


async def test_grep_empty_tokens(fake_bundle: Path) -> None:
    out = await grep_excerpts(bundle_path=fake_bundle, tokens=[])
    assert out == ()


async def test_grep_excerpt_includes_context_lines(fake_bundle: Path) -> None:
    """Default ±10 line window captures surrounding context."""
    out = await grep_excerpts(bundle_path=fake_bundle, tokens=["rblnDestroyContext"])
    excerpt = out[0].excerpt
    assert "// line 1" in excerpt  # window includes prefix
    assert "free(ctx);" in excerpt  # window includes suffix
