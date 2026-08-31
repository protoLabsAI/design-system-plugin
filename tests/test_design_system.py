"""design-system plugin — the pure logic (token-hex map, ds_check linting, drift diff),
with the two GitHub fetch seams (`_gh_get_raw` / `_gh_list`) monkeypatched. No network.

Loads the plugin's ``__init__.py`` by path (the repo dir isn't an importable package name).
Needs ``langchain_core`` (the @tool decorator) — run in the protoAgent venv."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("design_system_plugin", Path(__file__).resolve().parent.parent / "__init__.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


# A tiny stand-in token set with a couple of hex values at known paths.
_TOKENS = {
    "color": {
        "brand": {"lavender": "#9b87f2", "lavenderLight": "#b8a6f5"},
        "fg": {"muted": "#8a8f98"},
    },
    "radius": "8px",  # non-hex string — must not appear in the hex map
}


@pytest.fixture(autouse=True)
def _reset_cfg(monkeypatch, tmp_path):
    # Keep drift snapshots out of the real home dir.
    monkeypatch.setenv("DESIGN_SYSTEM_DIR", str(tmp_path / "ds"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)


def _call(tool, **kwargs):
    return tool.invoke(kwargs)


# ── hex → token map ───────────────────────────────────────────────────────────


def test_hex_token_map_only_hex_values():
    m = ds._hex_token_map(_TOKENS)
    assert m == {
        "#9b87f2": "color.brand.lavender",
        "#b8a6f5": "color.brand.lavenderLight",
        "#8a8f98": "color.fg.muted",
    }
    assert "8px" not in m  # non-hex string ignored


def test_component_names_from_story_listing():
    entries = [
        {"name": "Button.stories.tsx", "type": "file"},
        {"name": "Badge.stories.tsx", "type": "file"},
        {"name": "index.ts", "type": "file"},          # not a story
        {"name": "internal", "type": "dir"},
    ]
    assert ds._component_names(entries) == ["Badge", "Button"]


# ── ds_check ──────────────────────────────────────────────────────────────────


def test_ds_check_flags_a_hardcoded_token_color(monkeypatch):
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: json.dumps(_TOKENS))
    out = _call(ds.ds_check, code=".card { color: #9b87f2; background: #123456; }")
    assert "color.brand.lavender" in out          # known token → named
    assert "#9b87f2" in out
    assert "#123456" in out and "not a design token" in out  # unknown hex → flagged differently


def test_ds_check_clean_when_no_hardcoded_hex(monkeypatch):
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: json.dumps(_TOKENS))
    out = _call(ds.ds_check, code=".card { color: var(--pl-color-brand-lavender); }")
    assert "clean" in out.lower()


def test_ds_check_needs_input():
    assert "pass the code" in _call(ds.ds_check, code="   ")


# ── ds_components ─────────────────────────────────────────────────────────────


def test_ds_components_lists_inventory(monkeypatch):
    monkeypatch.setattr(ds, "_gh_list", lambda path: [
        {"name": "Button.stories.tsx"}, {"name": "AppShell.stories.tsx"}, {"name": "README.md"},
    ])
    out = _call(ds.ds_components)
    assert "- AppShell" in out and "- Button" in out and "README" not in out


# ── ds_drift ──────────────────────────────────────────────────────────────────


def test_ds_drift_baseline_then_detects_change(monkeypatch):
    state = {"tokens": json.dumps(_TOKENS), "components": [{"name": "Button.stories.tsx"}]}
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: state["tokens"])
    monkeypatch.setattr(ds, "_gh_list", lambda path: state["components"])

    first = _call(ds.ds_drift)
    assert "baseline" in first.lower()

    # No change → clean.
    assert "no change" in _call(ds.ds_drift).lower()

    # Change tokens + add a component → both reported.
    state["tokens"] = json.dumps({**_TOKENS, "color": {"brand": {"lavender": "#000000"}}})
    state["components"] = [{"name": "Button.stories.tsx"}, {"name": "Badge.stories.tsx"}]
    out = _call(ds.ds_drift)
    assert "TOKENS changed" in out
    assert "ADDED" in out and "Badge" in out


def test_fetch_error_becomes_tool_message(monkeypatch):
    def _boom(path):
        raise RuntimeError("could not fetch (ConnectError)")
    monkeypatch.setattr(ds, "_gh_get_raw", _boom)
    assert "ds_tokens error" in _call(ds.ds_tokens)


# ── design-critic subagent (v0.2.0) ───────────────────────────────────────────


def test_design_critic_subagent_shape():
    """The plugin builds a valid design-critic SubagentConfig grounded in the ds_* tools."""
    import sys, types
    # Stub graph.subagents.config so the builder imports without the full host.
    class _SC:
        def __init__(self, **kw): self.__dict__.update(kw)
    mod = types.ModuleType("graph.subagents.config"); mod.SubagentConfig = _SC
    pkg = types.ModuleType("graph.subagents"); sub = types.ModuleType("graph")
    sys.modules.setdefault("graph", sub); sys.modules.setdefault("graph.subagents", pkg)
    sys.modules["graph.subagents.config"] = mod

    sc = ds._build_design_critic()
    assert sc.name == "design-critic"
    # grounded in the live-DS tools, not general vibes
    for t in ("ds_rules", "ds_tokens", "ds_check", "ds_components", "ds_component"):
        assert t in sc.tools
    assert "review" in sc.description.lower() and "accessib" in sc.system_prompt.lower()
    assert "BLOCKER" in sc.system_prompt and "verdict" in sc.system_prompt.lower()


def test_headers_fall_back_to_gh_cli_token(monkeypatch):
    # Env empty → the gh CLI's token authorizes the read (desktop-app workspaces
    # inherit a Finder-launched env that never carries GITHUB_TOKEN/GH_TOKEN).
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(ds, "_CLI_TOKEN", "cli-tok-123")
    h = ds._headers("application/vnd.github.raw")
    assert h["Authorization"] == "Bearer cli-tok-123"


def test_headers_env_token_wins_over_cli(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    monkeypatch.setattr(ds, "_CLI_TOKEN", "cli-tok")
    h = ds._headers("application/vnd.github.raw")
    assert h["Authorization"] == "Bearer env-tok"


def test_headers_no_token_no_auth_header(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(ds, "_CLI_TOKEN", "")  # probed-and-absent → unauthenticated
    h = ds._headers("application/vnd.github.raw")
    assert "Authorization" not in h
# ── theme-designer engine (theme.py, loaded the same by-path way) ──
_tspec = importlib.util.spec_from_file_location("design_system_theme", Path(__file__).resolve().parent.parent / "theme.py")
th = importlib.util.module_from_spec(_tspec)
_tspec.loader.exec_module(th)


def test_scale_is_11_stops_light_to_dark():
    s = th.scale("#6366f1")
    assert len(s) == 11
    lightness = [th.cc.hex_to_lch(c)[0] for c in s]
    assert lightness[0] > 85 and lightness[-1] < 25
    assert all(a >= b - 1e-1 for a, b in zip(lightness, lightness[1:])), "lightness must be monotonically darkening"


def test_contrast_known_pairs():
    assert th.contrast("#000000", "#ffffff")["ratio"] == 21.0
    rep = th.contrast("#777777", "#888888")
    assert not rep["aa_normal"] and rep["ratio"] < 1.5


def test_harmony_rotations():
    comp = th.harmony("#ff0000", "complementary")
    assert len(comp) == 2
    tri = th.harmony("#ff0000", "triadic")
    assert len(tri) == 3
    import pytest as _pytest

    with _pytest.raises(ValueError):
        th.harmony("#ff0000", "square")


def test_palette_maps_to_pl_overrides_with_contrast():
    pal = th.palette("#6366f1", "triadic")
    mapped = th.overrides_for(pal, "dark")
    ov = mapped["overrides"]
    assert set(ov) >= {"--pl-color-bg", "--pl-color-fg", "--pl-color-accent", "--pl-color-status-error"}
    assert mapped["contrast"]["fg/bg"]["aa_normal"], "generated dark theme must pass AA for fg/bg"
    light = th.overrides_for(pal, "light")
    assert light["overrides"]["--pl-color-bg"] != ov["--pl-color-bg"]


def test_validate_overrides_gates_keys_and_warns_on_contrast():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        th.validate_overrides({"color": "#fff"})
    clean, warns = th.validate_overrides({"--pl-color-fg": "#888888", "--pl-color-bg": "#777777"})
    assert clean["--pl-color-fg"] == "#888888"
    assert any("FAILS WCAG AA" in w for w in warns)
    clean2, warns2 = th.validate_overrides({"--pl-radius": "12px"})
    assert clean2["--pl-radius"] == "12px" and any("not a parseable hex color" in w for w in warns2)


def test_colorcore_roundtrip_and_luminance():
    # hex → LCH → hex round-trips within 1/255 per channel for in-gamut colors
    for h in ("#6366f1", "#10b981", "#ef4444", "#0b0b10", "#f9fafb"):
        back = th.cc.lch_to_hex(th.cc.hex_to_lch(h))
        a = th.cc.parse_hex(h); b = th.cc.parse_hex(back)
        assert all(abs(x - y) <= 2 / 255 for x, y in zip(a, b)), (h, back)
    assert abs(th.cc.wcag_luminance((1.0, 1.0, 1.0)) - 1.0) < 1e-6
    assert th.cc.wcag_luminance((0.0, 0.0, 0.0)) == 0.0


# ── Storybook bridge ──────────────────────────────────────────────────────────

_sbspec = importlib.util.spec_from_file_location("design_system_storybook", Path(__file__).resolve().parent.parent / "storybook.py")
sb = importlib.util.module_from_spec(_sbspec)
_sbspec.loader.exec_module(sb)

# Shape mirrors a real Storybook index.json: story entries keyed by id, grouped by `title`.
_INDEX = {
    "v": 5,
    "entries": {
        "components-primitives-button--default": {
            "type": "story", "id": "components-primitives-button--default",
            "name": "Default", "title": "Components/Primitives/Button",
            "importPath": "./src/Button.stories.tsx",
        },
        "components-primitives-button--variants": {
            "type": "story", "id": "components-primitives-button--variants",
            "name": "Variants", "title": "Components/Primitives/Button",
            "importPath": "./src/Button.stories.tsx",
        },
        "components-overlays--toasts": {
            "type": "story", "id": "components-overlays--toasts",
            "name": "Toasts", "title": "Components/Overlays",
            "importPath": "./src/Overlays.stories.tsx",
        },
        "foundations--colors": {
            "type": "story", "id": "foundations--colors",
            "name": "Colors", "title": "Foundations",
            "importPath": "./src/Foundations.stories.tsx",
        },
        "components-overlays--docs": {
            "type": "docs", "id": "components-overlays--docs",
            "name": "Docs", "title": "Components/Overlays",
            "importPath": "./src/Overlays.stories.tsx",
        },
    },
}


def test_parse_index_folds_stories_into_components():
    comps = sb.parse_index(_INDEX)
    by_title = {c["title"]: c for c in comps}
    assert set(by_title) == {"Foundations", "Components/Overlays", "Components/Primitives/Button"}
    btn = by_title["Components/Primitives/Button"]
    assert btn["label"] == "Button" and btn["group"] == "Components/Primitives"
    assert [s["name"] for s in btn["stories"]] == ["Default", "Variants"]


def test_parse_index_separates_docs_entries_from_rendered_stories():
    """A docs entry has no visual, so the gallery must not offer it as a preview card."""
    overlays = next(c for c in sb.parse_index(_INDEX) if c["label"] == "Overlays")
    assert [s["name"] for s in overlays["stories"]] == ["Toasts"]
    assert [s["name"] for s in overlays["docs"]] == ["Docs"]


def test_ungrouped_component_has_empty_group():
    foundations = next(c for c in sb.parse_index(_INDEX) if c["label"] == "Foundations")
    assert foundations["group"] == ""


def test_preview_url_is_extensionless_by_default():
    """A Cloudflare Pages export 308s /iframe.html to /iframe; a local `storybook dev`
    serves ONLY /iframe.html. Default to the form that survives the redirect."""
    url = sb.preview_url("https://sb.example.com/", "button--primary")
    assert url.startswith("https://sb.example.com/iframe?id=button--primary")
    assert "viewMode=story" in url
    assert sb.preview_url("http://localhost:6006", "button--primary", legacy=True).startswith(
        "http://localhost:6006/iframe.html?id="
    )


def test_normalize_base_trims_trailing_slash():
    assert sb.normalize_base("  https://sb.example.com/ ") == "https://sb.example.com"


@pytest.mark.parametrize("needle", ["Button", "components/primitives/button", "BUTTON"])
def test_find_component_matches_title_label_and_case(needle):
    assert sb.find_component(sb.parse_index(_INDEX), needle)["label"] == "Button"


def test_find_component_misses_return_none():
    assert sb.find_component(sb.parse_index(_INDEX), "Nonexistent") is None
    assert sb.find_component(sb.parse_index(_INDEX), "") is None


def test_group_tree_buckets_by_group():
    tree = {n["group"]: n for n in sb.group_tree(sb.parse_index(_INDEX))}
    assert set(tree) == {"", "Components", "Components/Primitives"}


def test_summarize_lists_every_component_and_variant():
    out = sb.summarize(sb.parse_index(_INDEX))
    assert "3 components / 4 stories" in out
    assert "Button: Default, Variants" in out


# ── token CSS parsing ─────────────────────────────────────────────────────────

_tkspec = importlib.util.spec_from_file_location("design_system_tokens", Path(__file__).resolve().parent.parent / "tokens.py")
tk = importlib.util.module_from_spec(_tkspec)
_tkspec.loader.exec_module(tk)

# Mirrors the real generated file: a bare :root carrying the FULL set, a light @media
# wrapper with its own nested :root, and explicit [data-theme] force blocks.
_CSS = """
/* GENERATED from src/tokens.js by scripts/build.mjs — do not edit by hand. */
:root {
  color-scheme: dark;
  --pl-color-bg: #0a0a0c;
  --pl-color-fg: #ededed;
  --pl-color-brand-lavender: #9b87f2;
  --pl-space-4: 16px;
  --pl-motion-fast: 120ms;
  --pl-font-sans: "Geist", system-ui, sans-serif;
  --pl-shadow-popover: 0 8px 28px rgba(0, 0, 0, 0.5);
  --pl-gradient-brand: linear-gradient(135deg, #9b87f2 0%, #6366f1 100%);
}
@media (prefers-color-scheme: light) {
  :root {
    --pl-color-bg: #f6f7f9;
    --pl-color-fg: #18181b;
  }
}
/* Explicit theme force — wins over the OS preference above. */
:root[data-theme="light"] {
  --pl-color-bg: #f6f7f9;
  --pl-color-fg: #18181b;
}
:root[data-theme="dark"] {
  --pl-color-bg: #0a0a0c;
  --pl-color-fg: #ededed;
}
"""


def test_parse_css_splits_dark_and_light():
    th = tk.parse_css(_CSS)
    assert th["dark"]["--pl-color-bg"] == "#0a0a0c"
    assert th["light"]["--pl-color-bg"] == "#f6f7f9"
    # A token the light theme never overrides must still be present in it.
    assert th["light"]["--pl-space-4"] == "16px"
    assert th["light"]["--pl-color-brand-lavender"] == "#9b87f2"


def test_parse_css_is_independent_of_block_order():
    """REGRESSION: a flat scan folds the light @media's nested :root into the dark set,
    and only looks right while a later [data-theme="dark"] block happens to undo it.
    Reordering the DS's output must not flip dark to light."""
    blocks = [b for b in _CSS.strip().split("\n}\n") if b.strip()]
    shuffled = "\n}\n".join(reversed(blocks)) + "\n}\n"
    assert tk.parse_css(shuffled)["dark"]["--pl-color-bg"] == "#0a0a0c"
    assert tk.parse_css(shuffled)["light"]["--pl-color-bg"] == "#f6f7f9"


def test_parse_css_ignores_leading_comment_in_selector():
    """The generated banner comment sits directly before `:root`; if it rides along in the
    captured selector text the full base block is silently dropped."""
    assert len(tk.parse_css(_CSS)["dark"]) == 8


@pytest.mark.parametrize(
    "value,kind",
    [
        ("#9b87f2", "color"),
        ("rgba(255, 255, 255, 0.08)", "color"),
        ("oklch(0.72 0.13 145)", "color"),
        ("linear-gradient(135deg, #9b87f2 0%, #6366f1 100%)", "gradient"),
        ("0 8px 28px rgba(0, 0, 0, 0.5)", "shadow"),
        ("16px", "length"),
        ("120ms", "duration"),
        ('"Geist", system-ui, sans-serif', "font"),
        ("440", "number"),
        ("ease-in-out", "keyword"),
    ],
)
def test_classify_covers_every_token_kind(value, kind):
    """A gradient carries color stops and a shadow carries an rgba(), so both must be
    settled before the generic color test."""
    assert tk.classify(value) == kind


def test_group_tokens_orders_sections_and_flags_themed():
    th = tk.parse_css(_CSS)
    secs = tk.group_tokens(th["dark"], th["light"])
    # The fixture has no radius/border tokens, so assert the exact sections it DOES yield,
    # in the order a foundations page should read them.
    assert [s["section"] for s in secs] == [
        "Color", "Gradient", "Elevation", "Typography", "Space", "Motion",
    ]
    colors = next(s for s in secs if s["section"] == "Color")["tokens"]
    by_var = {t["var"]: t for t in colors}
    assert by_var["--pl-color-bg"]["themed"] is True
    assert by_var["--pl-color-brand-lavender"]["themed"] is False
    assert by_var["--pl-color-bg"]["light"] == "#f6f7f9"


def test_group_tokens_strips_the_pl_prefix_for_display():
    secs = tk.group_tokens(tk.parse_css(_CSS)["dark"])
    assert any(t["name"] == "color-bg" for s in secs for t in s["tokens"])


def test_token_summarize_notes_the_light_value_only_when_it_differs():
    out = tk.summarize(tk.group_tokens(*[tk.parse_css(_CSS)[k] for k in ("dark", "light")]))
    assert "var(--pl-color-bg) = #0a0a0c   (light: #f6f7f9)" in out
    assert "var(--pl-space-4) = 16px\n" in out


# ── ds_stories / ds_story tools + the catalog route ───────────────────────────


@pytest.fixture
def _sb(monkeypatch):
    """Serve the fixture index in place of the network, cache cleared."""
    monkeypatch.setattr(ds, "_SB_CACHE", None, raising=False)
    monkeypatch.setattr(ds, "_sb_components", lambda force=False: sb.parse_index(_INDEX))
    return ds


def test_ds_stories_returns_the_inventory(_sb):
    out = _call(ds.ds_stories)
    assert "3 components / 4 stories" in out
    assert "Button: Default, Variants" in out


def test_ds_story_lists_variants_with_live_urls(_sb):
    out = _call(ds.ds_story, name="Button")
    assert "Components/Primitives/Button — 2 variant(s)" in out
    assert "iframe?id=components-primitives-button--default" in out
    assert "./src/Button.stories.tsx" in out


def test_ds_story_unknown_name_points_at_the_inventory(_sb):
    assert "ds_stories" in _call(ds.ds_story, name="Nope")


def test_ds_stories_surfaces_a_fetch_failure(monkeypatch):
    def boom(force=False):
        raise RuntimeError("could not fetch the Storybook index")
    monkeypatch.setattr(ds, "_sb_components", boom)
    assert "ds_stories error" in _call(ds.ds_stories)


def test_catalog_isolates_a_token_failure_from_the_gallery(monkeypatch, _sb):
    """Tokens and stories come from different origins. One being down must not blank the
    other — a DS with no published Storybook should still browse its tokens, and a repo
    the plugin can't read shouldn't hide a gallery that loads fine."""
    def boom():
        raise RuntimeError("repo unreachable")
    monkeypatch.setattr(ds, "_token_sections", boom)
    catalog = next(r for r in ds._build_data_router().routes if r.path == "/catalog").endpoint
    out = catalog()
    assert out["tokens_error"] == "repo unreachable"
    assert out["tokens"] == []
    assert out["components_error"] is None
    assert [g["group"] for g in out["groups"]] == ["", "Components", "Components/Primitives"]


def test_catalog_isolates_a_gallery_failure_from_the_tokens(monkeypatch):
    def boom(force=False):
        raise RuntimeError("no storybook_url configured")
    monkeypatch.setattr(ds, "_sb_components", boom)
    monkeypatch.setattr(ds, "_token_sections", lambda: [{"section": "Color", "tokens": []}])
    out = next(r for r in ds._build_data_router().routes if r.path == "/catalog").endpoint()
    assert out["components_error"] == "no storybook_url configured"
    assert out["groups"] == []
    assert out["tokens"] and out["tokens_error"] is None


def test_catalog_attaches_a_preview_url_to_every_story(_sb):
    out = next(r for r in ds._build_data_router().routes if r.path == "/catalog").endpoint()
    stories = [s for g in out["groups"] for c in g["components"] for s in c["stories"]]
    assert stories and all(s["preview"].startswith("http") and "viewMode=story" in s["preview"] for s in stories)
    assert all("?path=/story/" in s["docs"] for s in stories)


def test_view_router_serves_the_path_the_manifest_declares():
    """Rule 1 of a plugin view: a mismatch between the manifest path and the served route
    is a blank iframe with no error anywhere."""
    import yaml

    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "protoagent.plugin.yaml").read_text())
    declared = manifest["views"][0]["path"]
    assert declared == "/plugins/design-system" + next(r.path for r in ds._build_view_router().routes)


# ── view page bootstrap ───────────────────────────────────────────────────────


def _view_html() -> str:
    return (Path(__file__).resolve().parent.parent / "view.html").read_text()


def test_kit_stylesheet_is_linked_statically_not_assigned_from_js():
    """An href filled in by script means the browser paints ONCE before the kit CSS is even
    requested — with no --pl-* tokens defined, i.e. a full-page white flash (measured at
    luminance 254 for ~90ms). The link must carry a real href in the markup."""
    html = _view_html()
    assert 'href="../../_ds/plugin-kit.css"' in html
    assert 'href=""' not in html
    assert '.href = base + "/_ds/plugin-kit.css"' not in html


def test_relative_kit_href_depth_matches_the_declared_view_path():
    """The `../../` depth is coupled to how deep the view path is. Move the view and the
    stylesheet 404s, which degrades to an unstyled page rather than an error."""
    import re

    import yaml

    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "protoagent.plugin.yaml").read_text())
    view_path = manifest["views"][0]["path"]              # e.g. /plugins/design-system/view
    depth = len([seg for seg in view_path.strip("/").split("/")[:-1]])  # dirs above the page
    hops = len(re.findall(r"\.\./", re.search(r'href="([^"]*_ds/plugin-kit\.css)"', _view_html()).group(1)))
    assert hops == depth, f"view path is {depth} deep but the kit href climbs {hops}"


def test_painted_surfaces_carry_a_literal_fallback():
    """Until the kit resolves, every var(--pl-*) is undefined — and an undefined custom
    property in a background is transparent, which paints white. The surfaces that cover
    the viewport need a literal fallback so a slow or failed kit lands on the right ground."""
    html = _view_html()
    for surface in ("--pl-color-bg, #0a0a0c", "--pl-color-bg-raised, #131316"):
        assert f"var({surface})" in html, f"missing fallback for {surface}"


def test_frames_reveal_on_storybook_render_not_on_load():
    """Storybook is client-rendered: `load` fires ~290ms BEFORE the story paints (measured
    load 391ms / storyRendered 683ms), and the document is blank white in between. Revealing
    on load put a one-frame flash of pure white in every card."""
    html = _view_html()
    assert "storyRendered" in html
    assert 'd.key !== "storybook-channel"' in html
    # `load` may only start the grace timer — never reveal directly.
    assert "requestAnimationFrame(show)" not in html
    assert "GRACE_AFTER_LOAD_MS" in html


def test_message_listener_checks_the_origin():
    """postMessage is receivable by anyone; only our configured Storybook may flip a card."""
    assert "e.origin !== new URL(origin).origin" in _view_html()


# ── playground pane ───────────────────────────────────────────────────────────


def test_cards_have_one_height_and_no_size_toggle():
    """Large is the default; the header control and the tall/compact tiers are gone."""
    html = _view_html()
    assert ".well { position: relative; height: 560px" in html
    assert 'id="big"' not in html and "body.big" not in html
    assert "card.tall" not in html and "const TALL" not in html


def test_playground_tab_exists():
    html = _view_html()
    assert 'data-tab="playground"' in html
    assert 'id="playground"' in html


def test_playground_drives_the_real_story_over_storybooks_channel():
    """Controls manipulate the live component through the same message Storybook's own
    manager sends — not a reimplementation of its rendering."""
    html = _view_html()
    assert '"updateStoryArgs"' in html
    assert '"resetStoryArgs"' in html
    assert '"updateGlobals"' in html
    assert 'd.event.type === "storyPrepared"' in html or '"storyPrepared"' in html


def test_playground_posts_to_the_storybook_origin_not_a_wildcard():
    """postMessage with "*" would leak the payload to whatever document is framed."""
    html = _view_html()
    assert "new URL(DATA.meta.storybook_url).origin," in html
    assert '"*"' not in html, "a wildcard targetOrigin would post to whatever is framed"


def test_width_and_theme_do_not_rerender_the_playground():
    """REGRESSION: re-rendering recreates the iframe, which reloads the story and silently
    discards every arg the user set — so checking a state at mobile width lost the state."""
    html = _view_html()
    click = html[html.index('$("playground").addEventListener("click"'):html.index('$("playground").addEventListener("input"')]
    width_branch = click[click.index('#pg-width'):click.index('#pg-theme')]
    assert "renderPlayground()" not in width_branch
    theme_branch = click[click.index('const t = e.target.closest("#pg-theme'):click.index("resetStoryArgs")]
    assert "renderPlayground()" not in theme_branch


def test_telejson_duplicate_markers_are_resolved_not_spread():
    """Storybook dedupes repeated structures into `_duplicate_["path"]` STRINGS. Spreading
    one as an object yields a char map, which rendered `<Button 0="_" 1="d" …>`."""
    html = _view_html()
    assert "_duplicate_" in html and "function undup(" in html
    assert "isPlainObject(live) ? live : prep.initialArgs" in html


def test_playground_fills_the_viewport_without_page_scroll():
    """`height: 100%` on .pg resolves against an auto-height block unless every ancestor is
    a zero-min flex box — which is what left the stage short with dead space beneath it."""
    html = _view_html()
    assert "body.pg-full .main { overflow: hidden; display: flex; flex-direction: column;" in html
    assert "body.pg-full #playground { flex: 1 1 auto; min-height: 0; display: flex; }" in html
    assert "body.pg-full .pg { flex: 1 1 auto; }" in html
    assert 'classList.toggle("pg-full", isPg)' in html
    # A min-height floor on the stage would reintroduce page scroll on a short viewport.
    assert "min-height: 320px" not in html


def test_gallery_keeps_its_own_scrolling():
    """The full-height switch is scoped to the playground — a gallery of many cards must
    still scroll, so .main stays a scroll container everywhere else."""
    html = _view_html()
    assert ".main { flex: 1 1 auto; overflow-y: auto;" in html


# ── ask (ds-explainer) ────────────────────────────────────────────────────────


def _ask_endpoint():
    return next(r for r in ds._build_data_router().routes if r.path == "/ask").endpoint


def _call_ask(body):
    import asyncio

    return asyncio.run(_ask_endpoint()(body))


@pytest.mark.parametrize("body", [{}, {"question": "   "}, None])
def test_ask_rejects_an_empty_question(body):
    out = _call_ask(body)
    assert out["ok"] is False and "Ask a question" in out["error"]


def test_ask_rejects_an_oversized_question():
    out = _call_ask({"question": "x" * 2001})
    assert out["ok"] is False and "too long" in out["error"]


def test_ask_injects_the_plugins_own_tools(monkeypatch):
    """REGRESSION: a subagent resolves its allowlist against the LEAD agent's bound tool map,
    which a plugin route plays no part in building — so without injecting them explicitly the
    call degrades to 'No tools available for subagent', with nothing saying why."""
    seen = {}

    async def fake(subagent_type, prompt, *, description, extra_tools=None, **kw):
        seen.update(type=subagent_type, prompt=prompt, tools=[t.name for t in (extra_tools or [])])
        return "answer"

    import sys, types
    mod = types.ModuleType("graph.sdk")
    mod.run_subagent = fake
    monkeypatch.setitem(sys.modules, "graph.sdk", mod)

    out = _call_ask({"question": "which button for delete?"})
    assert out["ok"] is True and out["answer"] == "answer"
    assert seen["type"] == "ds-explainer"
    assert seen["tools"], "no tools injected — the subagent would report 'No tools available'"
    assert {"ds_rules", "ds_tokens", "ds_stories"} <= set(seen["tools"])


def test_explainer_allowlist_is_derived_from_the_injected_tools():
    """One source: the allowlist can't drift from what the route actually injects."""
    import sys, types

    stub = types.ModuleType("graph.subagents.config")

    class SubagentConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    stub.SubagentConfig = SubagentConfig
    pkg = types.ModuleType("graph.subagents")
    pkg.config = stub
    sys.modules.setdefault("graph", types.ModuleType("graph"))
    sys.modules["graph.subagents"] = pkg
    sys.modules["graph.subagents.config"] = stub

    cfg = ds._build_explainer()
    assert cfg.name == "ds-explainer"
    assert cfg.tools == [t.name for t in ds._explainer_tools()]


def test_ask_surfaces_a_failure_instead_of_500ing(monkeypatch):
    import sys, types

    async def boom(*a, **kw):
        raise RuntimeError("gateway down")

    mod = types.ModuleType("graph.sdk")
    mod.run_subagent = boom
    monkeypatch.setitem(sys.modules, "graph.sdk", mod)
    out = _call_ask({"question": "anything"})
    assert out["ok"] is False and "gateway down" in out["error"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("[ds-explainer completed: Answer a design-system question]\n\nUse Button.", "Use Button."),
        ("No banner here.", "No banner here."),
        ("[craft completed: x] body", "body"),
    ],
)
def test_subagent_banner_is_stripped(raw, expected):
    """The dispatcher banner names which delegate answered — noise in a pane that only ever
    shows this one."""
    assert ds._strip_subagent_banner(raw) == expected


def test_answer_is_escaped_before_formatting():
    """The answer is model-authored; it must be escaped and only then given inline forms."""
    html = _view_html()
    assert "const src = esc(text);" in html
    assert "Never insert it as raw HTML" in html
