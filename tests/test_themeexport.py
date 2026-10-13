"""Public export contract: actual palette output, both modes, hostile/invalid input."""
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("export_plugin", Path(__file__).resolve().parents[1] / "__init__.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


def export(source, format="dtcg"):
    return ds.theme_export.invoke({"source_json": json.dumps(source), "format": format})


def test_palette_round_trip_all_families_stops_and_roles():
    source = json.loads(ds.theme_palette.invoke({"base_color": "#9b87f2", "mode": "light"}))
    result = json.loads(export(source))["light"]
    assert set(result["palette"]) == set(source["palette"]["scales"])
    for family, stops in source["palette"]["scales"].items():
        assert set(result["palette"][family]) == set(stops)
        for stop, color in stops.items():
            leaf = result["palette"][family][stop]
            rgba = ds._vocab_mod().parse_color(color)
            assert leaf["$value"] == {"colorSpace": "srgb", "components": list(rgba[:3]), "alpha": rgba[3]}
            assert leaf["$type"] == "color"
            assert leaf["$extensions"]["studio.protolabs"]["mode"] == "light"
            assert leaf["$extensions"]["studio.protolabs"]["css"] == color
    assert set(result["semantic"]) == set(source["overrides"])


def test_css_both_modes_and_single_light():
    source = {"dark": {"--pl-color-bg": "#123"}, "light": {"--pl-color-bg": "#fff", "--pl-color-fg": "rgb(0 0 0 / 80%)"}}
    assert export(source, "css") == ':root {\n  --pl-color-bg: #123;\n}\n\n[data-theme="light"] {\n  --pl-color-bg: #fff;\n  --pl-color-fg: rgb(0 0 0 / 80%);\n}\n'
    assert export({"mode": "light", "overrides": source["light"]}, "css").startswith(":root {")


def test_legacy_preserves_literal_and_mode_and_alpha_dtcg():
    source = {"mode": "dark", "overrides": {"--pl-color-fg": "#1234"}}
    leaf = json.loads(export(source, "style-dictionary"))["dark"]["semantic"]["--pl-color-fg"]
    assert leaf == {"value": "#1234", "type": "color", "$extensions": {"mode": "dark"}}
    leaf = json.loads(export(source))["dark"]["semantic"]["--pl-color-fg"]
    assert leaf["$value"]["alpha"] == pytest.approx(0x44 / 255)


def test_css_palette_includes_all_scales_and_semantics():
    source = json.loads(ds.theme_palette.invoke({"base_color": "#9b87f2"}))
    css = export(source, "css")
    for name, value in source["overrides"].items():
        assert f"{name}: {value};" in css
    for family, stops in source["palette"]["scales"].items():
        for stop, value in stops.items():
            assert f"--pl-palette-{family}-{stop}: {value};" in css


@pytest.mark.parametrize("source", [
    {}, [], {"overrides": {}}, {"overrides": []},
    {"mode": "other", "overrides": {"--pl-color-fg": "#fff"}},
    {"overrides": {"--pl-color-fg": "var(--other)"}},
    {"overrides": {"--pl-radius": "8px"}},
    {"overrides": {"--pl-color-fg": "#fff; color:red"}},
    {"overrides": {"--pl-color-fg}": "#fff"}},
    {"overrides": {"--pl-color-fg": None}},
    {"overrides": {"--pl-color-fg": "#fff"}, "light": {"--pl-color-fg": "#000"}},
    {"overrides": {"--pl-color-fg": "#fff"}, "palette": {"scales": {"$type": {"50": "#fff"}}}},
    {"overrides": {"--pl-color-fg": "#fff"}, "palette": {"scales": {"primary": {"51": "#fff"}}}},
])
@pytest.mark.parametrize("format", ["dtcg", "css", "style-dictionary"])
def test_invalid_input_is_legible_and_never_partial(source, format):
    assert export(source, format).startswith("theme_export error:")


def test_bad_json_and_format():
    assert ds.theme_export.invoke({"source_json": "{"}).startswith("theme_export error:")
    assert export({"overrides": {"--pl-color-fg": "#fff"}}, "bad").startswith("theme_export error:")


def test_css_collision_is_not_silently_overwritten():
    source = {"overrides": {"--pl-palette-primary-50": "#000"}, "palette": {"scales": {"primary": {"50": "#fff"}}}}
    assert "collides" in export(source, "css")
