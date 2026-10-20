"""Inline design previews (ADR 0118 D5 frame component) — store, per-item sanitising, the tool
opt-in, registration, the frame page and its routes. Host-free: ``graph.sdk`` is stubbed."""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sqlite3
import sys
import threading
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("design_system_plugin_pv", ROOT / "__init__.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)
pv = ds._previews_mod()
theme = ds._theme_mod()
SENTINEL = "\x00component:"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DESIGN_SYSTEM_DIR", str(tmp_path / "ds"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    # graph.sdk.encode_component, stubbed: the real one prefixes a sentinel + JSON.
    graph = types.ModuleType("graph")
    sdk = types.ModuleType("graph.sdk")
    sdk.encode_component = lambda name, props: SENTINEL + json.dumps({"component": name, "props": props})
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    monkeypatch.setattr(ds, "_PREVIEWS_ENABLED", True)


def _component(out: str) -> dict | None:
    i = out.find(SENTINEL)
    return json.loads(out[i + len(SENTINEL):]) if i >= 0 else None


# ── props validator ───────────────────────────────────────────────────────────


def test_validate_ref_accepts_only_a_pointer():
    good = {"preview_id": "a" * 32, "title": "Palette"}
    assert pv.validate_ref(good) is None
    assert pv.validate_ref({**good, "groups": []})  # content never rides chat history
    assert pv.validate_ref({"preview_id": "A" * 32, "title": "x"})
    assert pv.validate_ref({"preview_id": "a" * 31, "title": "x"})
    assert pv.validate_ref({"preview_id": "a" * 32, "title": ""})
    assert pv.validate_ref({"preview_id": "a" * 32, "title": "x" * 201})
    assert pv.validate_ref("nope")


# ── colors: server and client agree by construction ─────────────────────────────


@pytest.mark.parametrize(
    "raw,want",
    [
        ("#fff", "#ffffff"),
        ("#FFFA", "#ffffffaa"),
        ("#123456", "#123456"),
        ("#12345678", "#12345678"),
        ("rgb(255 0 0)", "#ff0000"),
        ("rgba(0, 0, 0, 0.5)", "#00000080"),
        ("oklch(0.7 0.1 250)", None),  # parsed, value checked below rather than pinned
    ],
)
def test_canonical_color_shapes(raw, want):
    got = pv.canonical_color(raw, theme.parse_rgba)
    assert got is not None and re.match(pv.COLOR_PATTERN, got), (raw, got)
    if want:
        assert got == want


@pytest.mark.parametrize("raw", ["#12345", "#1234567", "#ggg", "color-mix(in srgb, red, blue)", "currentColor", "var(--nope)", "", None, "x" * 300])
def test_canonical_color_refuses_what_it_cannot_pin(raw):
    assert pv.canonical_color(raw, theme.parse_rgba) is None


def test_var_resolves_through_the_token_set_and_its_fallback():
    lookup = {"--a": "var(--b)", "--b": "#00ff00", "--loop": "var(--loop)"}
    assert pv.canonical_color("var(--a)", theme.parse_rgba, lookup) == "#00ff00"
    assert pv.canonical_color("var(--missing, #f00)", theme.parse_rgba, lookup) == "#ff0000"
    assert pv.canonical_color("var(--loop)", theme.parse_rgba, lookup) is None  # bounded


def test_client_color_regex_is_the_server_pattern():
    html = (ROOT / "preview_card.html").read_text()
    m = re.search(r"var COLOR_RE = /(.+?)/;", html)
    assert m and m.group(1) == pv.COLOR_PATTERN


# ── per-item sanitising ───────────────────────────────────────────────────────


def test_a_bad_swatch_drops_only_itself():
    groups = [
        {"name": "Good", "swatches": [{"label": "a", "value": "#111111", "light": "#eeeeee", "copy": "var(--a)"},
                                      {"label": "bad", "value": "#12345"},
                                      {"label": "mix", "value": "color-mix(in srgb, red, blue)"},
                                      {"label": "", "value": "#222222"}]},
        {"name": "Empty", "swatches": [{"label": "x", "value": "nope"}]},
        {"name": "Light only bad", "swatches": [{"label": "b", "value": "#333", "light": "color-mix(in srgb, red, blue)"}]},
    ]
    payload, dropped = pv.palette_payload("P", groups, theme.parse_rgba)
    assert dropped == 4
    assert [g["name"] for g in payload["groups"]] == ["Good", "Light only bad"]
    assert payload["groups"][0]["swatches"] == [{"label": "a", "value": "#111111", "light": "#eeeeee", "copy": "var(--a)"}]
    assert payload["groups"][1]["swatches"] == [{"label": "b", "value": "#333333"}]  # bad light face → pair dropped, swatch kept
    assert payload["dropped"] == 4


def test_palette_with_nothing_drawable_is_none():
    assert pv.palette_payload("P", [{"name": "x", "swatches": [{"label": "a", "value": "nope"}]}], theme.parse_rgba) == (None, 1)


def test_palette_bounds():
    groups = [{"name": f"g{i}", "swatches": [{"label": f"s{j}", "value": "#000000"} for j in range(40)]} for i in range(20)]
    payload, dropped = pv.palette_payload("P", groups, theme.parse_rgba)
    assert len(payload["groups"]) <= pv.MAX_GROUPS
    assert sum(len(g["swatches"]) for g in payload["groups"]) == pv.MAX_SWATCHES
    assert dropped == 20 * 40 - pv.MAX_SWATCHES


def test_contrast_payload_paints_the_composited_foreground():
    res = theme.contrast("rgba(255,255,255,0.5)", "#000000")
    p = pv.contrast_payload("rgba(255,255,255,0.5)", "#000000", res, theme.parse_rgba)
    assert p["foreground"] == res["composited_foreground"] and p["background"] == "#000000"
    assert p["foreground_alpha"] == 0.5 and p["ratio"] == res["ratio"]


def test_component_payload_drops_a_story_that_fails_the_frame_check():
    sb = ds._sb_mod()
    comp = {"title": "Components/Button", "label": "Button", "stories": [
        {"id": "button--primary", "name": "Primary"}, {"id": "", "name": "No id"}]}
    payload, dropped = pv.component_payload(comp, "https://sb.example.com", sb.preview_url, sb.docs_url)
    assert dropped == 1 and [s["id"] for s in payload["stories"]] == ["button--primary"]
    bad = lambda base, sid: "https://evil.example/iframe?id=" + sid
    assert pv.component_payload(comp, "https://sb.example.com", bad, sb.docs_url)[0] is None


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://u:p@sb.example", "https://sb.example/?x=1", "ftp://sb.example", ""])
def test_safe_storybook_refuses(url):
    with pytest.raises(ValueError):
        pv.safe_storybook(url)


# ── the store ─────────────────────────────────────────────────────────────────


def test_save_read_roundtrip_and_pragmas(tmp_path):
    ref = pv.save(tmp_path, {"kind": "contrast", "title": "C", "foreground": "#000000"})
    assert pv.validate_ref(ref) is None
    snap = pv.read(tmp_path, ref["preview_id"])
    assert snap["version"] == 1 and snap["created_at"].endswith("Z")
    with sqlite3.connect(tmp_path / "previews.db") as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert pv.read(tmp_path, "0" * 32) is None
    assert pv.read(tmp_path, "../etc") is None


def test_busy_timeout_is_set_before_wal():
    src = inspect.getsource(pv._connect)
    assert src.index("busy_timeout") < src.index("journal_mode")


def test_retention_caps_rows_and_age(tmp_path):
    t0 = 1_700_000_000.0
    old = pv.save(tmp_path, {"kind": "palette", "title": "old"}, now=t0)
    ids = [pv.save(tmp_path, {"kind": "palette", "title": f"n{i}"}, now=t0 + 100 * 86400 + i, max_rows=3)["preview_id"] for i in range(5)]
    assert pv.read(tmp_path, old["preview_id"]) is None  # aged out
    assert [pv.read(tmp_path, i) is not None for i in ids] == [False, False, True, True, True]  # newest 3 kept


def test_concurrent_saves_do_not_lock(tmp_path):
    errors: list[Exception] = []

    def work():
        try:
            for _ in range(20):
                pv.save(tmp_path, {"kind": "palette", "title": "t"})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    with sqlite3.connect(tmp_path / "previews.db") as db:
        assert db.execute("SELECT COUNT(*) FROM previews").fetchone()[0] == 120


def test_migrates_the_draft_table_without_created(tmp_path):
    with sqlite3.connect(tmp_path / "previews.db") as db:
        db.execute("CREATE TABLE previews (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("INSERT INTO previews VALUES (?, ?)", ("b" * 32, json.dumps({"kind": "palette"})))
    ref = pv.save(tmp_path, {"kind": "palette", "title": "new"})
    assert pv.read(tmp_path, ref["preview_id"])["title"] == "new"


def test_oversized_snapshot_refused(tmp_path):
    with pytest.raises(ValueError, match="too large"):
        pv.save(tmp_path, {"kind": "palette", "title": "x", "blob": "x" * (pv.MAX_BYTES + 1)})


# ── tools: opt-in, never at the cost of the answer ─────────────────────────────


@pytest.mark.parametrize("tool", ["ds_tokens", "ds_story", "theme_scale", "theme_contrast", "theme_palette"])
def test_show_preview_is_opt_in(tool):
    t = getattr(ds, tool)
    assert t.args_schema.model_json_schema()["properties"]["show_preview"]["default"] is False
    assert "show_preview" in t.description


def test_theme_contrast_without_preview_is_unchanged():
    out = ds.theme_contrast.invoke({"foreground": "#000", "background": "#fff"})
    assert _component(out) is None and json.loads(out)["ratio"] == 21.0


def test_theme_contrast_with_preview_saves_a_snapshot():
    out = ds.theme_contrast.invoke({"foreground": "#777", "background": "#fff", "show_preview": True})
    comp = _component(out)
    assert comp["component"] == "design-preview" and pv.validate_ref(comp["props"]) is None
    snap = pv.read(ds._preview_root(), comp["props"]["preview_id"])
    assert snap["kind"] == "contrast" and snap["foreground"] == "#777777" and snap["aa_normal"] is False


def test_theme_palette_and_scale_previews():
    out = ds.theme_palette.invoke({"base_color": "#6d5dfc", "show_preview": True})
    snap = pv.read(ds._preview_root(), _component(out)["props"]["preview_id"])
    assert snap["kind"] == "palette" and snap["groups"][0]["swatches"][0]["light"]
    out = ds.theme_scale.invoke({"base_color": "#6d5dfc", "show_preview": True})
    snap = pv.read(ds._preview_root(), _component(out)["props"]["preview_id"])
    assert len(snap["groups"][0]["swatches"]) == 11


def test_ds_tokens_preview_resolves_aliases(monkeypatch):
    css = ":root{--pl-color-accent:#8b7cf6;--pl-color-focus:var(--pl-color-accent);--pl-color-mix:color-mix(in srgb, red, blue);--pl-space-1:4px}"
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: css)
    out = ds.ds_tokens.invoke({"section": "Color", "show_preview": True})
    snap = pv.read(ds._preview_root(), _component(out)["props"]["preview_id"])
    sw = {s["label"]: s for s in snap["groups"][0]["swatches"]}
    assert sw["--pl-color-focus"]["value"] == "#8b7cf6" and sw["--pl-color-focus"]["copy"] == "var(--pl-color-focus)"
    assert "--pl-color-mix" not in sw and snap["dropped"] == 1
    assert "omitted" in out


def test_ds_story_preview(monkeypatch):
    comp = {"title": "Components/Button", "label": "Button", "stories": [{"id": "components-button--primary", "name": "Primary"}]}
    monkeypatch.setattr(ds, "_sb_components", lambda force=False: [comp])
    out = ds.ds_story.invoke({"name": "Button", "show_preview": True})
    snap = pv.read(ds._preview_root(), _component(out)["props"]["preview_id"])
    assert snap["kind"] == "component" and snap["stories"][0]["preview"].startswith(ds._cfg("storybook_url"))
    assert _component(ds.ds_story.invoke({"name": "Button"})) is None


def test_a_failing_store_keeps_the_answer(monkeypatch):
    monkeypatch.setattr(ds, "_preview_root", lambda: (_ for _ in ()).throw(OSError("disk full")))
    out = ds.theme_contrast.invoke({"foreground": "#000", "background": "#fff", "show_preview": True})
    assert json.loads(out.split("\n")[0])["ratio"] == 21.0 and "unavailable" in out and _component(out) is None


def test_no_frame_host_means_no_component(monkeypatch):
    monkeypatch.setattr(ds, "_PREVIEWS_ENABLED", False)
    out = ds.theme_contrast.invoke({"foreground": "#000", "background": "#fff", "show_preview": True})
    assert _component(out) is None and "no frame-component support" in out


# ── registration + manifest + routes ───────────────────────────────────────────


def _manifest() -> dict:
    return yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())


class _Reg:
    def __init__(self, frames=True):
        self.config = {"watch_cron": ""}
        self.plugin_id = "design-system"
        self.emit = None
        self.routers, self.tools, self.components = [], [], []
        if frames:
            self.register_component = self._rc
        else:
            self.register_component = lambda name, validator: None  # pre-0.202 shape

    def _rc(self, name, validator, frame=None, reads=None):
        self.components.append((name, validator, frame, reads))

    def register_router(self, router, prefix=""):
        self.routers.append((router, prefix))

    def register_tools(self, tools):
        self.tools.extend(tools)

    def register_subagent(self, cfg):
        pass


def test_register_declares_the_frame_component(monkeypatch):
    monkeypatch.setattr(ds, "_CFG", dict(ds._DEFAULTS))
    monkeypatch.setattr(ds, "_build_design_critic", lambda: None)
    monkeypatch.setattr(ds, "_build_explainer", lambda: None)
    monkeypatch.setattr(ds, "_build_designer", lambda: None)
    reg = _Reg()
    ds.register(reg)
    assert ds._PREVIEWS_ENABLED is True
    (name, validator, frame, reads), = reg.components
    assert name == "design-preview" and validator is pv.validate_ref
    # Core's _accept_component_frame: relative, no '..', covered by the manifest public_paths.
    full = f"/plugins/design-system/{frame}"
    assert not frame.startswith("/") and any(full.startswith(p) for p in _manifest()["public_paths"])
    # The read pattern names the data route the frame reads.
    assert reads == ["previews/*"]
    assert "/previews/{preview_id}" in {r.path for r in ds._build_data_router().routes}


def test_register_on_a_host_without_frames_turns_previews_off(monkeypatch):
    monkeypatch.setattr(ds, "_CFG", dict(ds._DEFAULTS))
    for b in ("_build_design_critic", "_build_explainer", "_build_designer"):
        monkeypatch.setattr(ds, b, lambda: None)
    ds.register(_Reg(frames=False))
    assert ds._PREVIEWS_ENABLED is False


def test_min_version_is_the_frame_component_release():
    assert _manifest()["min_protoagent_version"] == "0.202.0"


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ds._build_view_router(), prefix="/plugins/design-system")
    app.include_router(ds._build_data_router(), prefix="/api/plugins/design-system")
    return TestClient(app)


def test_frame_page_is_served_at_the_public_path_with_a_locked_csp(monkeypatch):
    monkeypatch.setitem(ds._CFG, "storybook_url", "https://sb.example.com/")
    c = _client()
    r = c.get("/plugins/design-system" + ds.PREVIEW_FRAME_PATH)
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in r.text and "__NONCE__" not in r.text
    assert "frame-src https://sb.example.com;" in csp and "connect-src 'none'" in csp
    assert c.get("/plugins/design-system" + ds.PREVIEW_FRAME_PATH).headers["content-security-policy"] != csp  # fresh nonce
    assert c.get("/api/plugins/design-system" + ds.PREVIEW_FRAME_PATH).status_code == 404  # page is not a data route


def test_frame_csp_without_a_storybook_embeds_nothing(monkeypatch):
    monkeypatch.setitem(ds._CFG, "storybook_url", "")
    assert "frame-src 'none'" in _client().get("/plugins/design-system/preview-card").headers["content-security-policy"]


def test_preview_data_route():
    ref = pv.save(ds._preview_root(), {"kind": "palette", "title": "t"})
    c = _client()
    assert c.get(f"/api/plugins/design-system/previews/{ref['preview_id']}").json()["title"] == "t"
    r = c.get("/api/plugins/design-system/previews/" + "0" * 32)
    assert r.status_code == 404 and "no longer available" in r.json()["detail"]


def test_frame_page_follows_the_protocol_and_builds_dom_safely():
    html = (ROOT / "preview_card.html").read_text()
    assert 'type: "protoComponent:ready", pair: pair' in html
    assert "e.source !== parent" in html
    assert "nonce: nonce" in html  # echoed on every read/openLink
    assert "protoComponent:openLink" in html  # popups are sandboxed off; the host opens links
    assert "innerHTML" not in html and not re.search(r"\son[a-z]+=", html)  # no markup injection, no inline handlers
    assert html.count('role="status"') == 1  # ONE live region, not one per swatch
    assert 'button[aria-pressed="true"]' in html  # a visible pressed state
    assert "protoComponent:height" in html
