"""themegen — brand extraction + theme generation against the token contract. No network: the
URL path's fetch seam (``_tg_fetch``) and the contract fetch (``_gh_get_raw``) are monkeypatched.

The contract fixture is a trimmed copy of the real protoContent ``dist/tokens.css`` (4-block
shape, every color role, chart series 1–4)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_FIX = _HERE / "fixtures"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tg = _load("design_system_themegen_t", _ROOT / "themegen.py")
tk = _load("design_system_tokens_t", _ROOT / "tokens.py")
ds = _load("design_system_plugin_tg", _ROOT / "__init__.py")

CONTRACT_CSS = (_FIX / "tokens_contract.css").read_text()
CONTRACT = tk.parse_css(CONTRACT_CSS)
COLOR_VARS = [v for v in CONTRACT["dark"] if v.startswith("--pl-color-")]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("DESIGN_SYSTEM_DIR", str(tmp_path / "ds"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)


# ── color parsing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, hexv, alpha",
    [
        ("#9b87f2", "#9b87f2", 1.0),
        ("#fff", "#ffffff", 1.0),
        ("#00000080", "#000000", 128 / 255),
        ("rgba(255, 255, 255, 0.08)", "#ffffff", 0.08),
        ("rgb(20 20 30 / 35%)", "#14141e", 0.35),
        ("hsl(0, 100%, 50%)", "#ff0000", 1.0),
        ("white", "#ffffff", 1.0),
        ("color(srgb 1 0 0)", "#ff0000", 1.0),
    ],
)
def test_parse_color_forms(value, hexv, alpha):
    rgb, a = tg.parse_color(value)
    assert tg.cc.to_hex(rgb) == hexv
    assert a == pytest.approx(alpha, abs=0.01)


def test_parse_color_oklch_matches_a_known_value():
    # oklch(0.628 0.2577 29.23) is sRGB red — the canonical OKLCH reference value.
    rgb, _ = tg.parse_color("oklch(0.628 0.2577 29.23)")
    assert tg._delta_e(tg.cc.to_hex(rgb), "#ff0000") < 2


@pytest.mark.parametrize("value", ["var(--x)", "currentColor", "linear-gradient(red, blue)", "nonsense", ""])
def test_parse_color_rejects_non_literals(value):
    assert tg.parse_color(value) is None


# ── extraction ────────────────────────────────────────────────────────────────


def test_extract_light_site_ranks_the_brand_first_with_evidence():
    res = tg.extract_from_css((_FIX / "site_light.css").read_text())
    top = res["brand"][0]
    assert tg._delta_e(top["hex"], "#0f766e") < 7
    assert any("--brand-primary" in e for e in top["evidence"])
    assert any("btn-primary" in e for e in top["evidence"])
    assert res["background"]["mode"] == "light"
    assert res["background"]["hex"] == "#ffffff"
    assert tg._delta_e(res["foreground"]["hex"], "#1e293b") < 7
    assert "Inter" in res["fonts"][0]["stack"]
    assert res["radius"]["mode"] == "6px" and res["radius"]["pill"] >= 1
    s = res["suggested"]
    assert tg._delta_e(s["primary"], "#0f766e") < 7 and s["site_mode"] == "light"


def test_extract_dark_site_finds_dark_ground_brand_and_secondary():
    res = tg.extract_from_css((_FIX / "site_dark.css").read_text())
    assert res["background"]["mode"] == "dark"
    assert tg._delta_e(res["brand"][0]["hex"], "#7c3aed") < 12  # violet family (merged with #6d28d9)
    hexes = [b["hex"] for b in res["brand"]]
    assert any(tg._delta_e(h, "#ec4899") < 7 for h in hexes)
    assert tg._delta_e(res["suggested"]["secondary"], "#ec4899") < 7
    assert res["radius"]["mode"] == "12px"
    assert "Satoshi" in res["fonts"][0]["stack"]
    # Neutrals are reported separately, never as brand candidates.
    assert all(b["lch"][1] >= 12 for b in res["brand"])


def test_extract_from_computed_probe_shape():
    probe = (_FIX / "probe_result.json").read_text()
    res = tg.extract_from_computed(probe)
    assert res["source"] == "computed" and res["url"] == "https://acme.example/"
    assert res["brand"][0]["hex"] == "#e11d48"
    assert any("theme-color" in e for e in res["brand"][0]["evidence"])
    assert res["background"]["hex"] == "#ffffff" and res["background"]["mode"] == "light"
    assert res["radius"]["mode"] == "8px"
    assert "Manrope" in res["fonts"][0]["stack"]
    # A double-encoded probe (JSON.stringify output passed as a JSON string) reads the same.
    assert tg.extract_from_computed(json.dumps(probe))["brand"][0]["hex"] == "#e11d48"


def test_extract_from_colors_keeps_operator_order():
    res = tg.extract_from_colors("#f59e0b, #0f766e, #64748b")
    assert res["suggested"]["primary"] == "#f59e0b"
    assert res["suggested"]["secondary"] == "#0f766e"


@pytest.mark.parametrize(
    "src, kind",
    [("https://acme.com", "url"), ("acme.com", "url"), ('{"colors": []}', "probe"), ("#fff, #000", "colors"), ("a{color:red}", "css")],
)
def test_classify_source(src, kind):
    assert tg.classify_source(src) == kind


def test_probe_js_is_a_defensive_self_contained_expression():
    js = tg.PROBE_JS
    assert js.startswith("(() =>") and js.rstrip().endswith("})()")
    assert "cssRules" in js and "catch" in js  # cross-origin sheets throw — must be caught
    assert "JSON.stringify" in js and "19000" in js  # size-capped
    for key in ("themeColorMeta", "bodyBg", "bodyFg", "customProps", "fonts", "radii", "area"):
        assert key in js
    assert "fetch(" not in js and "XMLHttpRequest" not in js


def test_site_assets_reads_links_meta_inline_and_style_attrs():
    html = """<html><head><meta name="theme-color" content="#123456">
    <link rel="stylesheet" href="/css/site.css"><link rel="stylesheet" href="https://evil.example/x.css">
    <style>.hero{background:#ff0000}</style></head>
    <body><a class="btn go" style="background-color:#00aa00">Go</a></body></html>"""
    a = tg.site_assets(html, "https://www.acme.com/page")
    assert a["theme_color"] == "#123456"
    assert "https://www.acme.com/css/site.css" in a["stylesheets"]
    assert "#ff0000" in a["inline_css"] and "a.btn.go[style]" in a["inline_css"]
    assert tg.stylesheet_allowed("https://cdn.acme.com/a.css", "https://www.acme.com/")
    assert tg.stylesheet_allowed("https://fonts.googleapis.com/css2?x", "https://www.acme.com/")
    assert not tg.stylesheet_allowed("https://evil.example/x.css", "https://www.acme.com/")


# ── generation ────────────────────────────────────────────────────────────────

AWKWARD = ["#ffff00", "#39ff14", "#0b0b0f", "#ffc0cb", "#808080", "#e11d48", "#0f766e"]


def _gen(primary, **kw):
    return tg.generate(tg.seeds_from_brand(primary), CONTRACT, **kw)


def test_role_inference_covers_the_contract_without_a_hardcoded_list():
    roles = {v: tg.infer_role(v, CONTRACT["dark"][v]) for v in COLOR_VARS}
    assert roles["--pl-color-bg"] == "ground"
    assert roles["--pl-color-bg-raised"] == "surface"
    assert roles["--pl-color-fg-on-accent"] == "on-accent"
    assert roles["--pl-color-accent-fg"] == "accent-text"
    assert roles["--pl-color-accent-hover"] == "accent-variant"
    assert roles["--pl-color-status-warning"] == "status"
    assert roles["--pl-color-chart-series1"] == "chart-series"
    assert roles["--pl-color-chart-axis"] == "text-muted"
    assert roles["--pl-color-brand-lavender"] == "brand-mark"
    # Unknown names fall back by value: alpha → line, opaque → hue-shift.
    assert tg.infer_role("--pl-color-sparkle", "#ff00aa") == "hue-shift"
    assert tg.infer_role("--pl-color-sparkle", "rgba(0,0,0,.2)") == "line"


def test_generate_covers_every_color_var_in_both_themes():
    th = _gen("#e11d48")
    assert set(th["dark"]) == set(COLOR_VARS)
    assert set(th["light"]) == set(COLOR_VARS)
    assert th["passthrough"] == []
    for theme in ("dark", "light"):
        for v, val in th[theme].items():
            assert tg.parse_color(val), f"{theme} {v} = {val!r} is not a color"


def test_generate_picks_up_a_var_the_contract_adds():
    extended = {t: {**CONTRACT[t], "--pl-color-sparkle": "#9b87f2"} for t in ("dark", "light")}
    th = tg.generate(tg.seeds_from_brand("#0f766e"), extended)
    assert "--pl-color-sparkle" in th["dark"] and th["roles"]["--pl-color-sparkle"] == "hue-shift"


@pytest.mark.parametrize("seed", AWKWARD)
def test_every_pair_passes_aa_for_awkward_seeds(seed):
    th = _gen(seed)
    assert th["contrast"], "no pairs were checked"
    bad = [r for r in th["contrast"] if r["status"] == "fail" or r["ratio"] < r["need"]]
    assert not bad, bad[:5]
    # Re-verify from the emitted values, not the recorded ratio.
    for r in th["contrast"]:
        assert tg.ratio(th[r["theme"]][r["fg"]], th[r["theme"]][r["bg"]]) >= r["need"]


def test_body_text_pairs_are_held_to_4_5():
    th = _gen("#39ff14")
    fg_pairs = [r for r in th["contrast"] if r["fg"] in ("--pl-color-fg", "--pl-color-fg-muted", "--pl-color-accent-fg", "--pl-color-fg-on-accent")]
    assert fg_pairs and all(r["need"] == 4.5 for r in fg_pairs)


def test_elevation_relation_follows_the_ds():
    th = _gen("#0f766e")
    L = lambda t, v: tg._lch(th[t][v])[0]
    assert L("dark", "--pl-color-bg-raised") > L("dark", "--pl-color-bg")  # dark: raised is lighter
    assert L("light", "--pl-color-bg-raised") >= max(L("light", v) for v in ("--pl-color-bg", "--pl-color-bg-subtle", "--pl-color-bg-hover"))


def test_neutrals_are_tinted_toward_the_brand_but_low_chroma():
    th = _gen("#e11d48")
    for t in ("dark", "light"):
        _, C, _ = tg._lch(th[t]["--pl-color-bg"])
        assert C < 6
    assert abs(tg._hue_delta(tg._lch(th["light"]["--pl-color-bg"])[2], tg._lch("#e11d48")[2])) < 25


def test_yellow_accent_is_stepped_deeper_on_light_and_reported():
    th = _gen("#ffff00")
    assert th["dark"]["--pl-color-accent"] == "#ffff00"  # clears on dark as-is
    light_acc = th["light"]["--pl-color-accent"]
    assert light_acc != "#ffff00" and tg.ratio(light_acc, th["light"]["--pl-color-bg"]) >= 3
    assert any(a["theme"] == "light" and a["var"] == "--pl-color-accent" and "scale" in a["reason"] for a in th["adjustments"])
    # Dark text on a yellow fill, light text on the deep step.
    assert tg._lch(th["dark"]["--pl-color-fg-on-accent"])[0] < 20
    assert tg._lch(th["light"]["--pl-color-fg-on-accent"])[0] > 90


def test_repairs_are_reported_with_before_after_and_reason():
    th = _gen("#ffc0cb")
    assert th["repairs"], "the DS's own light warning fails 3:1 — a repair is expected"
    for r in th["repairs"]:
        assert set(r) >= {"theme", "var", "from", "to", "reason"} and r["from"] != r["to"]
    assert any(r["status"] == "repaired" for r in th["contrast"])
    rep = tg.contrast_report(th)
    assert "repaired" in rep and "FAIL" in rep  # the header counts fails (0)


def test_status_colors_stay_recognizable():
    th = _gen("#7c3aed")
    for t in ("dark", "light"):
        for name, default in (("success", 145), ("error", 25), ("info", 245)):
            h = tg._lch(th[t][f"--pl-color-status-{name}"])[2]
            d = tg._lch(tg._hex(CONTRACT[t][f"--pl-color-status-{name}"]))[2]
            assert abs(tg._hue_delta(d, h)) <= 12


def test_strict_status_raises_status_to_4_5():
    th = _gen("#0f766e", strict_status=True)
    st = [r for r in th["contrast"] if "status" in r["fg"]]
    assert st and all(r["need"] == 4.5 and r["ratio"] >= 4.5 for r in st)


def test_monochrome_brand_keeps_default_chart_hues():
    th = _gen("#808080")
    d = tg._lch(tg._hex(CONTRACT["dark"]["--pl-color-chart-series3"]))[2]
    assert abs(tg._hue_delta(d, tg._lch(th["dark"]["--pl-color-chart-series3"])[2])) < 3


def test_seeds_from_brand_validates():
    assert tg.seeds_from_brand("0f766e")["primary"] == "#0f766e"
    assert tg.seeds_from_brand("rgb(255,0,0)")["primary"] == "#ff0000"
    for translucent in ("transparent", "rgba(255, 0, 0, 0.5)", "#ff000080"):
        with pytest.raises(ValueError, match="translucent"):
            tg.seeds_from_brand(translucent)
    with pytest.raises(ValueError):
        tg.seeds_from_brand("")
    with pytest.raises(ValueError):
        tg.seeds_from_brand("#0f766e", secondary="blurple")


# ── rendering ─────────────────────────────────────────────────────────────────


def test_render_css_has_the_ds_4_block_shape_and_parses_back():
    th = _gen("#e11d48", font_family='"Manrope"', radius="8")
    css = th and tg.render_css(th)
    assert css.count("@media (prefers-color-scheme: light)") == 1
    assert ':root[data-theme="light"] {' in css and ':root[data-theme="dark"] {' in css
    assert re.search(r"^:root \{", css, re.MULTILINE)
    back = tk.parse_css(css)
    for t in ("dark", "light"):
        for v in COLOR_VARS:
            assert back[t][v] == th[t][v], (t, v)
    assert back["dark"]["--pl-radius"] == "8px"
    assert back["dark"]["--pl-font-sans"].startswith('"Manrope"')
    # The themed blocks carry only what differs between themes (brand marks are invariant).
    light_block = css.split(':root[data-theme="light"] {', 1)[1].split("}", 1)[0]
    assert "--pl-color-brand-lavender" not in light_block and "--pl-color-bg:" in light_block


def test_render_css_scoped_for_white_label():
    th = _gen("#0f766e")
    css = tg.render_css(th, '[data-brand="acme"]')
    assert re.search(r'^\[data-brand="acme"\] \{', css, re.MULTILINE)
    assert ':root[data-theme="light"] [data-brand="acme"], [data-brand="acme"][data-theme="light"]' in css
    assert "@media (prefers-color-scheme: light) {\n  [data-brand=\"acme\"] {" in css
    # Structurally identical to the :root render once the scope is swapped back.
    swapped = css.replace(':root[data-theme="light"] [data-brand="acme"], [data-brand="acme"][data-theme="light"]', ':root[data-theme="light"]')
    swapped = swapped.replace(':root[data-theme="dark"] [data-brand="acme"], [data-brand="acme"][data-theme="dark"]', ':root[data-theme="dark"]')
    swapped = swapped.replace('[data-brand="acme"]', ":root")
    assert tk.parse_css(swapped)["light"]["--pl-color-bg"] == th["light"]["--pl-color-bg"]
    with pytest.raises(ValueError):
        tg.render_css(th, "x{} body")


def test_render_json_maps_are_theme_apply_ready():
    th = _gen("#0f766e")
    data = json.loads(tg.render_json(th))
    clean, _ = ds._theme_mod().validate_overrides(data["light"])
    assert clean["--pl-color-accent"] == th["light"]["--pl-color-accent"]


def test_preview_html_is_self_contained_and_uses_ds_vocabulary():
    th = _gen("#e11d48")
    html = tg.preview_html(th)
    assert html.startswith("<!doctype html>")
    assert "<script" not in html and "http://" not in html and "https://" not in html
    for cls in ("pl-btn", "pl-alert", "pl-badge", "pl-card", "pl-input"):
        assert cls in html
    assert ".tg-dark{" in html and ".tg-light{" in html
    assert th["dark"]["--pl-color-accent"] in html and th["light"]["--pl-color-accent"] in html
    assert "var(--pl-color-chart-series1)" in html
    assert "Contrast" in html and "--pl-radius:" in html  # non-color defaults travel with it
    assert len(html) < 120_000


# ── tools ─────────────────────────────────────────────────────────────────────


def _call(t, **kw):
    return t.invoke(kw)


def test_theme_generate_writes_files_against_the_live_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: CONTRACT_CSS)
    out = _call(ds.theme_generate, primary="#0f766e", name="Acme Co!", scope='[data-brand="acme"]')
    assert "Theme 'Acme-Co'" in out and "show_artifact" in out and "theme_apply" in out
    d = tmp_path / "ds" / "themes"
    css = (d / "Acme-Co.theme.css").read_text()
    assert '[data-brand="acme"]' in css
    assert json.loads((d / "Acme-Co.theme.json").read_text())["dark"]
    assert (d / "Acme-Co.preview.html").read_text().startswith("<!doctype html>")
    assert "FAIL" in out and "0 FAIL" in out


def test_theme_generate_reports_a_bad_seed_and_a_bad_contract(monkeypatch):
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: CONTRACT_CSS)
    assert "error" in _call(ds.theme_generate, primary="blurple")

    def boom(path):
        raise RuntimeError("could not fetch tokens.css")

    monkeypatch.setattr(ds, "_gh_get_raw", boom)
    assert "token contract" in _call(ds.theme_generate, primary="#0f766e")


def test_theme_extract_url_path_uses_the_fetch_seam(monkeypatch):
    pages = {
        "https://www.acme.com/": '<html><head><link rel="stylesheet" href="/s.css"><link rel="stylesheet" href="https://tracker.example/t.css"></head><body></body></html>',
        "https://www.acme.com/s.css": (_FIX / "site_light.css").read_text(),
    }
    calls = []

    def fake(url, max_bytes, deadline):
        calls.append(url)
        return url, pages[url]

    monkeypatch.setattr(ds, "_tg_fetch", fake)
    out = _call(ds.theme_extract, source="https://www.acme.com/")
    assert calls == ["https://www.acme.com/", "https://www.acme.com/s.css"]  # off-site sheet skipped
    assert "#0f766e" in out and "browser_eval" in out and "1 skipped" in out


def test_theme_extract_other_sources():
    assert "#e11d48" in _call(ds.theme_extract, source=(_FIX / "probe_result.json").read_text())
    assert "primary=#f59e0b" in _call(ds.theme_extract, source="#f59e0b, #0f766e")
    assert "Brand candidates" in _call(ds.theme_extract, source=(_FIX / "site_dark.css").read_text())
    assert "theme_extract error" in _call(ds.theme_extract, source="{\"not\": \"closed\"")


def test_fetch_seam_refuses_private_hosts():
    import time

    for url in ("http://127.0.0.1:7870/api/secrets", "http://100.119.239.8:8765/", "http://[::1]:7870/"):
        with pytest.raises(RuntimeError, match="non-public"):
            ds._tg_fetch(url, 1000, time.monotonic() + 5)
    assert "theme_extract error" in _call(ds.theme_extract, source="http://127.0.0.1:7870/")


def test_probe_script_tool_returns_the_probe():
    out = _call(ds.theme_probe_script)
    assert "browser_eval" in out and tg.PROBE_JS in out


def test_new_tools_are_registered():
    class Reg:
        config = {"watch_cron": ""}  # noqa: RUF012 — a stand-in registry, read-only
        plugin_id = "design-system"

        def __init__(self):
            self.tools = []

        def register_router(self, *a, **k):
            pass

        def register_tools(self, tools):
            self.tools.extend(tools)

        def register_subagent(self, *a, **k):
            pass

    r = Reg()
    ds.register(r)
    names = {t.name for t in r.tools}
    assert {"theme_extract", "theme_probe_script", "theme_generate", "theme_scale", "theme_contrast", "theme_palette", "theme_apply"} <= names


def test_body_background_beats_html_background():
    res = tg.extract_from_css("html { background-color: #2b5b84; } body { background-color: #ffffff; color: #444; }")
    assert res["background"]["hex"] == "#ffffff" and res["background"]["mode"] == "light"


def test_brand_marks_follow_primary_and_a_secondary_family():
    th = tg.generate(tg.seeds_from_brand("#3776ab", "#ffd343"), CONTRACT)
    assert th["dark"]["--pl-color-brand-lavender"] == "#3776ab"  # = the DS accent → exactly the primary
    assert th["dark"]["--pl-color-brand-indigo"] == "#ffd343"    # second family's base → the secondary
    assert th["dark"]["--pl-color-brand-indigo-deep"] == th["light"]["--pl-color-brand-indigo-deep"]  # invariant
    solo = _gen("#3776ab")
    assert abs(tg._hue_delta(tg._lch(solo["dark"]["--pl-color-brand-indigo"])[2], tg._lch("#3776ab")[2])) < 15



# ── review round 1 regressions ────────────────────────────────────────────────


@pytest.mark.parametrize("font", [
    "Inter;} body{display:none} :root{--x:1",
    "Inter</style><script>alert(document.domain)</script><style>",
    'Inter", url(//evil.example/x)',
    "Inter /* x */",
])
def test_font_family_injection_is_rejected(font, monkeypatch):
    with pytest.raises(ValueError, match="font stack"):
        _gen("#e11d48", font_family=font)
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: CONTRACT_CSS)
    assert "theme_generate error" in _call(ds.theme_generate, primary="#e11d48", font_family=font)


def test_plain_font_stacks_pass_validation():
    th = _gen("#e11d48", font_family='"Source Sans Pro", Arial, sans-serif')
    assert th["base"]["--pl-font-sans"] == '"Source Sans Pro", Arial, sans-serif'
    th = _gen("#e11d48", font_family="Inter")
    assert th["base"]["--pl-font-sans"].startswith("Inter, ")


def test_preview_style_block_cannot_be_closed_by_a_value():
    th = _gen("#e11d48")
    th["base"] = {"--pl-font-sans": "x</style><script>alert(1)</script>"}  # bypassing generate on purpose
    for compact in (False, True):
        html = tg.preview_html(th, compact=compact)
        assert html.count("</style>") == 1 and "</style><script" not in html  # only the real closing tag


def test_comma_scope_is_qualified_part_by_part():
    css = tg.render_css(_gen("#0f766e"), ".a, .b")
    assert re.search(r"^\.a, \.b \{", css, re.MULTILINE)
    assert ':root[data-theme="light"] .a, .a[data-theme="light"], :root[data-theme="light"] .b, .b[data-theme="light"] {' in css
    assert "@media (prefers-color-scheme: light) {\n  .a, .b {" in css


def test_html_scope_maps_to_root_and_root_compounds_qualify_in_place():
    th = _gen("#0f766e")
    assert tg.render_css(th, "html") == tg.render_css(th, ":root")
    css = tg.render_css(th, "html.acme")
    assert re.search(r"^:root\.acme \{", css, re.MULTILINE)
    assert ':root.acme[data-theme="light"] {' in css
    assert ":root[data-theme" not in css.split("Explicit")[1].replace(':root.acme[data-theme', "")


@pytest.mark.parametrize("bad", ["x{} body", ".a,, .b", ".a; color:red", "<b>", ".a /* c */"])
def test_bad_scopes_are_rejected(bad):
    with pytest.raises(ValueError):
        tg.render_css(_gen("#0f766e"), bad)


def test_scoped_render_round_trips_through_parse_css():
    th = _gen("#0f766e")
    for scope in ('[data-brand="acme"]', ".a, .b"):
        back = tk.parse_css(tg.render_css(th, scope), scope=tg.normalize_scope(scope))
        for t in ("dark", "light"):
            assert all(back[t][v] == th[t][v] for v in COLOR_VARS), (scope, t)


def test_theme_json_carries_font_radius_and_apply_maps():
    th = _gen("#0f766e", font_family="Inter", radius="6")
    data = json.loads(tg.render_json(th))
    assert data["base"]["--pl-radius"] == "6px"
    assert data["apply"]["light"]["--pl-radius"] == "6px"
    assert data["apply"]["dark"]["--pl-color-accent"] == th["dark"]["--pl-color-accent"]


def test_strict_holds_placeholder_text_to_4_5_and_default_says_why():
    th = _gen("#0f766e", strict=True)
    sub = [r for r in th["contrast"] if r["fg"] == "--pl-color-fg-subtle"]
    assert sub and all(r["need"] == 4.5 and r["ratio"] >= 4.5 for r in sub)
    assert not th["notes"]
    assert any("placeholder" in n for n in _gen("#0f766e")["notes"])


@pytest.mark.parametrize("seed", ["#60ad9e", "#73e3ce", "#25e2df", "#f4f205", "#ffff00", "#0f766e", "#7c3aed", "#f97316"])
def test_chart_series_stay_distinguishable(seed):
    th = _gen(seed)
    series = [v for v in COLOR_VARS if "chart-series" in v]
    for t in ("dark", "light"):
        floor = max(12.0, 0.8 * tg._min_de([tg._hex(CONTRACT[t][v]) for v in series]))
        assert tg._min_de([th[t][v] for v in series]) >= floor - 0.5


# ── confidence (M5) + probe ranking (M6) ──


def test_a_single_inline_custom_prop_is_not_high_confidence():
    res = tg.extract_from_css(':root{--testimonial-accent-color: green}', source="url", sheets_fetched=0)
    assert res["suggested"]["confidence"] == "low"
    res = tg.extract_from_css(':root{--testimonial-accent-color: green}')
    assert res["suggested"]["confidence"] != "high"


def test_high_confidence_needs_two_independent_sources():
    css = ":root{--brand:#0f766e} .btn-primary{background:var(--brand)} a{color:#0f766e} body{background:#fff;color:#111}"
    assert tg.extract_from_css(css)["suggested"]["confidence"] == "high"
    only_selectors = ".x1{background:#0f766e}.x2{background:#0f766e}.x3{background:#0f766e} body{background:#fff}"
    assert tg.extract_from_css(only_selectors)["suggested"]["confidence"] != "high"


def test_status_and_text_named_props_are_not_brand():
    for name in ("--brand-color-danger-fg", "--primary-text", "--accent-border", "--brand-success", "--color-accent-muted"):
        assert not tg.is_brand_prop(name), name
    for name in ("--brand-primary", "--color-accent", "--primary", "--brand"):
        assert tg.is_brand_prop(name), name


def test_probe_does_not_pick_status_or_body_text_as_brand():
    probe = {
        "bodyBg": "rgb(255, 255, 255)", "bodyFg": "rgb(6, 27, 49)",
        "colors": [
            {"value": "rgb(255, 255, 255)", "prop": "background", "area": 2_000_000, "count": 50},
            {"value": "rgb(6, 27, 49)", "prop": "color", "area": 900_000, "count": 400},     # navy body text
            {"value": "rgb(66, 84, 102)", "prop": "color", "area": 300_000, "count": 200},   # slate text
            {"value": "rgb(83, 58, 253)", "prop": "button", "area": 20_000, "count": 5},     # the real brand
            {"value": "rgb(83, 58, 253)", "prop": "link", "area": 5_000, "count": 12},
        ],
        "customProps": {"--brand-color-danger-fg": "#fa383d", "--brand-color-success-fg": "#1a7f37"},
    }
    res = tg.extract_from_computed(probe)
    assert res["brand"][0]["hex"] == "#533afd"
    assert all(tg._delta_e(b["hex"], "#fa383d") > 7 or b["score"] < res["brand"][0]["score"] * 0.3 for b in res["brand"])
    assert not any(tg._delta_e(b["hex"], "#061b31") < 7 for b in res["brand"])  # the ink is the foreground, not a brand


@pytest.mark.parametrize("probe", [
    {"customProps": ["--a", "#fff"]},
    {"colors": 5},
    {"colors": [{"value": "#fff", "area": "lots"}]},
    {"fonts": {"family": "x"}},
    [1, 2, 3],
])
def test_malformed_probe_shapes_are_a_legible_error(probe):
    with pytest.raises(ValueError):
        tg.extract_from_computed(json.dumps(probe))
    if isinstance(probe, dict):  # a JSON array isn't routed to the probe path at all
        assert "theme_extract error" in _call(ds.theme_extract, source=json.dumps(probe))


# ── the hand-off (M4) ──


def test_theme_generate_returns_inline_artifact_apply_maps_and_preview_url(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: CONTRACT_CSS)
    out = _call(ds.theme_generate, primary="#0f766e", name="acme", font_family="Inter")
    frag = out.split("<<<ARTIFACT_HTML\n", 1)[1].split("\nARTIFACT_HTML>>>", 1)[0]
    assert frag.startswith("<style>") and "pl-btn" in frag and "tg-dark" in frag
    assert len(frag) < 16_000  # compact: no contrast table, minified CSS
    dark = json.loads(out.split("\ntheme_apply dark: ", 1)[1].split("\n", 1)[0])
    light = json.loads(out.split("\ntheme_apply light: ", 1)[1].split("\n", 1)[0])
    assert dark["--pl-font-sans"].startswith("Inter") and light["--pl-color-accent"]
    clean, _ = ds._theme_mod().validate_overrides(light)  # what theme_apply runs before persisting
    assert clean["--pl-color-accent"] == light["--pl-color-accent"]
    assert "/plugins/design-system/themes/acme/preview" in out


def test_preview_route_serves_the_generated_page(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: CONTRACT_CSS)
    _call(ds.theme_generate, primary="#0f766e", name="acme")
    app = FastAPI()
    app.include_router(ds._build_view_router(), prefix="/plugins/design-system")
    c = TestClient(app)
    r = c.get("/plugins/design-system/themes/acme/preview")
    assert r.status_code == 200 and "Contrast" in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert c.get("/plugins/design-system/themes/nope/preview").status_code == 404
    assert c.get("/plugins/design-system/themes/..%2Fsnapshot/preview").status_code in (400, 404)


def test_data_dir_prefers_the_host_plugin_store(monkeypatch, tmp_path):
    import sys
    import types

    monkeypatch.delenv("DESIGN_SYSTEM_DIR", raising=False)
    store = tmp_path / "instance" / "plugins" / "design-system"
    fake = types.ModuleType("graph.sdk")
    fake.plugin_store = lambda subdir="", *, plugin_id: (store.mkdir(parents=True, exist_ok=True), store)[1]
    monkeypatch.setitem(sys.modules, "graph", types.ModuleType("graph"))
    monkeypatch.setitem(sys.modules, "graph.sdk", fake)
    monkeypatch.setattr(ds.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert ds._themes_dir() == store / "themes"
    assert ds._snap_path() == store / "snapshot.json"


def test_live_stripe_probe_picks_the_violet_not_the_navy_ink():
    """A real PROBE_JS capture from stripe.com (2026-09): navy #061b31 is body text + button
    text; the brand is the violet button fill."""
    res = tg.extract_from_computed((_FIX / "probe_stripe_live.json").read_text())
    s = res["suggested"]
    assert s["primary"] == "#533afd"
    assert tg._delta_e(s["secondary"] or "#ffffff", "#061b31") > 10
