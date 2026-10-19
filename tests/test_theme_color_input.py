"""theme_contrast / theme_scale take the CSS colors a browser computes, not just hex (#38).

An agent checking a live page reads colors from ``getComputedStyle``, which returns
``rgb(r, g, b)`` / ``rgba(r, g, b, a)``. Those must work as-is, and a translucent color
must be composited or refused, never silently treated as opaque."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


th = _load("design_system_theme_ci", _ROOT / "theme.py")
ds = _load("design_system_plugin_ci", _ROOT / "__init__.py")

HEX_RATIO = th.contrast("#e5e7eb", "#111827")["ratio"]


@pytest.mark.parametrize(
    "fg, bg",
    [
        ("#e5e7eb", "#111827"),
        ("e5e7eb", "111827"),  # legacy bare hex still works
        ("#E5E7EB", "#111827"),
        ("#e5e7ebff", "#111827ff"),
        ("rgb(229, 231, 235)", "rgb(17, 24, 39)"),
        ("rgb(229,231,235)", "rgb(17,24,39)"),
        ("  rgb( 229 , 231 , 235 )  ", "\trgb(17, 24, 39)\n"),
        ("rgb(229 231 235)", "rgb(17 24 39)"),
        ("RGB(229, 231, 235)", "Rgb(17, 24, 39)"),
        ("rgba(229, 231, 235, 1)", "rgba(17, 24, 39, 1)"),
        ("rgba(229, 231, 235, 100%)", "rgb(17 24 39 / 1)"),
        ("rgb(229 231 235 / 100%)", "rgb(17 24 39 / 1.0)"),
        ("rgb(229.0, 231.0, 235.0)", "rgb(17, 24, 39)"),
    ],
)
def test_every_form_of_the_same_color_gives_the_same_ratio(fg, bg):
    assert th.contrast(fg, bg)["ratio"] == HEX_RATIO


def test_percentage_channels():
    assert th.contrast("rgb(100%, 100%, 100%)", "rgb(0%, 0%, 0%)")["ratio"] == 21.0
    assert th.contrast("rgb(100% 100% 100%)", "#000")["ratio"] == 21.0
    # 50% → 127.5 of 255: between its two nearest hex neighbours
    mid = th.contrast("rgb(50%, 50%, 50%)", "#fff")["ratio"]
    assert th.contrast("#808080", "#fff")["ratio"] <= mid <= th.contrast("#7f7f7f", "#fff")["ratio"]


@pytest.mark.parametrize(
    "value, hexv",
    [
        ("oklch(1 0 0)", "#ffffff"),
        ("oklch(100% 0 0)", "#ffffff"),
        ("oklch(0 0 0)", "#000000"),
        ("oklch(62.8% 0.2577 29.23)", "#ff0000"),
        ("oklch(0.628 0.2577 29.23deg)", "#ff0000"),
        ("hsl(0, 100%, 50%)", "#ff0000"),
        ("hsl(0 100% 50%)", "#ff0000"),
        ("hsla(120, 100%, 25%, 1)", "#008000"),
        ("white", "#ffffff"),
    ],
)
def test_oklch_hsl_and_named_match_hex(value, hexv):
    for bg in ("#000000", "#ffffff", "#336699"):
        # hex is quantised to 1/255 per channel; hsl(…25%) is 127.5, oklch is float math
        assert th.contrast(value, bg)["ratio"] == pytest.approx(th.contrast(hexv, bg)["ratio"], abs=0.05)


def test_translucent_foreground_is_composited_over_the_background():
    # 50% white over black is ~#808080, not white (which would read 21:1)
    rep = th.contrast("rgba(255, 255, 255, 0.5)", "#000000")
    assert rep["ratio"] == pytest.approx(th.contrast("#808080", "#000000")["ratio"], abs=0.05)
    assert rep["foreground_alpha"] == 0.5
    assert rep["composited_foreground"] in ("#7f7f7f", "#808080")
    # % alpha, space syntax and #rrggbbaa are the same 50%
    assert th.contrast("rgb(255 255 255 / 50%)", "#000")["ratio"] == rep["ratio"]
    # 0x80 is 128/255 ≈ 0.502, a hair above 50%
    assert th.contrast("#ffffff80", "#000")["ratio"] == pytest.approx(rep["ratio"], abs=0.05)


def test_fully_transparent_foreground_is_invisible():
    rep = th.contrast("rgba(0, 0, 0, 0)", "#ffffff")
    assert rep["ratio"] == 1.0 and not rep["aa_large"]
    assert th.contrast("transparent", "#ffffff")["ratio"] == 1.0


def test_opaque_result_shape_is_unchanged():
    assert set(th.contrast("#000", "#fff")) == {"ratio", "aa_normal", "aa_large", "aaa_normal"}


@pytest.mark.parametrize("bg", ["rgba(0, 0, 0, 0)", "transparent", "rgba(17, 24, 39, 0.9)", "#11182780", "rgb(0 0 0 / 50%)"])
def test_translucent_background_is_refused_clearly(bg):
    with pytest.raises(ValueError, match="translucent.*backdrop"):
        th.contrast("#ffffff", bg)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not-a-color",
        "#12",
        "#12345",
        "#gggggg",
        "rgb(1, 2)",
        "rgb(1, 2, 3, 4, 5)",
        "rgb(a, b, c)",
        "rgb(1px, 2, 3)",
        "rgb(1, 2, 3",
        "rgb(inf, 0, 0)",
        "rgb(nan, 0, 0)",
        "var(--pl-color-fg)",
        "currentColor",
        "linear-gradient(red, blue)",
    ],
)
def test_unparseable_input_names_the_accepted_forms(bad):
    with pytest.raises(ValueError, match=r"rgb\(\)/rgba\(\).*oklch"):
        th.contrast(bad, "#000000")


def test_out_of_range_channels_clamp_like_css():
    assert th.contrast("rgb(300, 300, 300)", "rgb(-10, -10, -10)")["ratio"] == 21.0
    assert th.contrast("rgba(255, 255, 255, 1.5)", "#000")["ratio"] == 21.0


def test_scale_and_palette_take_the_same_forms():
    assert th.scale("rgb(99, 102, 241)") == th.scale("#6366f1")
    assert th.harmony("rgb(255 0 0)", "triadic") == th.harmony("#ff0000", "triadic")
    assert th.palette("rgb(99, 102, 241)")["base"] == "#6366f1"
    with pytest.raises(ValueError, match="translucent"):
        th.scale("rgba(99, 102, 241, 0.5)")


def test_validate_overrides_accepts_css_colors_and_survives_a_translucent_bg():
    clean, warnings = th.validate_overrides({"--pl-color-fg": "rgb(229, 231, 235)", "--pl-color-bg": "oklch(0.2 0.02 260)"})
    assert not any("not a parseable" in w for w in warnings)
    _, warnings = th.validate_overrides({"--pl-color-fg": "#fff", "--pl-color-bg": "rgba(0, 0, 0, 0.5)"})
    assert any("contrast not checked" in w for w in warnings)


def test_tool_takes_computed_style_output_and_documents_it():
    out = json.loads(ds.theme_contrast.invoke({"foreground": "rgb(229, 231, 235)", "background": "rgb(17, 24, 39)"}))
    assert out["ratio"] == HEX_RATIO
    err = ds.theme_contrast.invoke({"foreground": "rgb(1, 2)", "background": "#000"})
    assert err.startswith("Error:") and "rgb()/rgba()" in err
    doc = ds.theme_contrast.description
    for form in ("rgb()/rgba()", "hsl()", "oklch()", "#rrggbbaa", "composited", "translucent background"):
        assert form in doc, form
