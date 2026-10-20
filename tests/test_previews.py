"""Saved previews and tool emissions, without a running host or network."""
import json
import sys
import types

import pytest
from fastapi import HTTPException

from test_design_system import ds


@pytest.fixture
def previews(monkeypatch, tmp_path):
    mod = ds._sibling("previews.py")
    monkeypatch.setattr(ds, "_PREVIEWS_ENABLED", True)
    monkeypatch.setattr(ds, "_preview_root", lambda: tmp_path)
    sdk = types.ModuleType("graph.sdk")
    sdk.encode_component = lambda kind, props: "\x1e[component-v1]" + json.dumps({"component": kind, "props": props})
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    return mod, tmp_path


def snapshot(previews, result):
    mod, root = previews
    text, wire = result.split("\x1e[component-v1]")
    part = json.loads(wire)
    assert part["component"] == "design-preview-ref"
    assert mod.validate_ref(part["props"]) is None
    assert set(part["props"]) == {"preview_id", "title"}
    return text.strip(), mod.read(root, part["props"]["preview_id"]), part["props"]["preview_id"]


def test_palette_snapshot_survives_later_palettes_and_routes(previews):
    text, saved, ident = snapshot(previews, ds.theme_palette.invoke({"base_color": "#663399"}))
    assert json.loads(text)["palette"]["base"] == "#663399"
    assert saved["groups"][0]["swatches"][0]["light"] != saved["groups"][0]["swatches"][0]["value"]
    ds.theme_palette.invoke({"base_color": "#ff0000"})
    route = next(r for r in ds._build_data_router().routes if r.path == "/previews/{preview_id}").endpoint
    assert route(ident) == saved
    with pytest.raises(HTTPException) as err:
        route("0" * 32)
    assert err.value.status_code == 404
    assert previews[0].read(previews[1], "../../secrets") is None


def test_contrast_and_scale_emit_real_values(previews):
    _, contrast, _ = snapshot(previews, ds.theme_contrast.invoke({"foreground": "#fff", "background": "#000"}))
    assert contrast["foreground"] == "#ffffff"
    assert contrast["ratio"] == 21
    assert contrast["aa_normal"] is True
    _, scale, _ = snapshot(previews, ds.theme_scale.invoke({"base_color": "#663399"}))
    assert len(scale["groups"][0]["swatches"]) == 11


def test_tokens_and_story_tools_emit_cards(previews, monkeypatch):
    monkeypatch.setattr(ds, "_token_sections", lambda: [{"section": "Color", "tokens": [
        {"var": "--pl-color-fg", "name": "fg", "value": "#ffffff", "light": "#000000", "kind": "color", "themed": True},
    ]}])
    _, tokens, _ = snapshot(previews, ds.ds_tokens.invoke({"section": "Color", "show_preview": True}))
    assert tokens["groups"][0]["swatches"][0]["copy"] == "var(--pl-color-fg)"
    monkeypatch.setattr(ds, "_sb_components", lambda: [{"title": "Primitives/Button", "label": "Button", "stories": [
        {"id": "button--primary", "name": "Primary"}, {"id": "button--ghost", "name": "Ghost"},
    ]}])
    monkeypatch.setitem(ds._CFG, "storybook_url", "https://stories.example.com")
    _, card, _ = snapshot(previews, ds.ds_story.invoke({"name": "Button"}))
    assert len(card["stories"]) == 2
    assert card["stories"][1]["preview"].startswith("https://stories.example.com/iframe?id=button--ghost")


def test_research_and_old_hosts_keep_plain_results(previews, monkeypatch):
    assert json.loads(ds.theme_contrast.invoke({"foreground": "#fff", "background": "#000", "show_preview": False}))["ratio"] == 21
    monkeypatch.setattr(ds, "_PREVIEWS_ENABLED", False)
    assert json.loads(ds.theme_scale.invoke({"base_color": "#663399"}))
    assert not (previews[1] / "previews.db").exists()


def test_failed_storage_preserves_tool_result(previews, monkeypatch):
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(previews[0], "save", fail)
    text = ds.theme_contrast.invoke({"foreground": "#fff", "background": "#000"})
    assert '"ratio": 21.0' in text and "Inline preview unavailable" in text
    assert "component-v1" not in text


def test_refs_and_payloads_are_bounded(previews):
    mod, root = previews
    assert mod.validate_ref({"preview_id": "../x", "title": "x"})
    assert mod.validate_ref({"preview_id": "a" * 32, "title": "x", "html": "<script>"})
    with pytest.raises(ValueError, match="too large"):
        mod.save(root, {"kind": "palette", "title": "Huge", "data": "x" * mod.MAX_BYTES})
    for base in ["javascript:alert(1)", "https://user:password@example.com", "https://example.com/?q=x"]:
        with pytest.raises(ValueError):
            mod.safe_storybook(base)
