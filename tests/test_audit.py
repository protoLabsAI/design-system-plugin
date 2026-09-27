"""The audit engine — vocab.py (token vocabulary), audit.py (rules, walker, report) and the
two tools on top (ds_check, ds_audit_repo). Host-free and network-free: the DS is the tiny
fixture under tests/fixtures/ds, the consumer the fixture tree under tests/fixtures/app."""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vocab = _load("ds_test_vocab", "vocab.py")
audit = _load("ds_test_audit", "audit.py")
cc = _load("ds_test_colorcore", "colorcore.py")
ds = _load("ds_test_plugin", "__init__.py")

TOKENS_CSS = (FIX / "ds" / "tokens.css").read_text()


@pytest.fixture(scope="module")
def V():
    return vocab.build_vocab(TOKENS_CSS)


def _run(code: str, filename: str, V, inventory=None, rules=None):
    ctx = audit.AuditContext(inventory=inventory, rules=rules)
    return audit.audit_text(code, filename, V, ctx=ctx), ctx


def _rules(findings):
    return [f["rule"] for f in findings]


# ── colorcore additions ───────────────────────────────────────────────────────


def test_delta_e_2000_reference_pair():
    # Sharma et al. 2005 test data, pair 1: ΔE00 = 2.0425.
    assert cc.delta_e_2000((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)) == pytest.approx(2.0425, abs=1e-3)
    assert cc.delta_e_2000((50, 0, 0), (50, 0, 0)) == 0


def test_oklch_decodes_to_srgb():
    # oklch(0.628 0.2577 29.23) is pure sRGB red.
    r, g, b = cc.oklch_to_rgb((0.62796, 0.25768, 29.2339))
    assert (round(r, 2), round(g, 2), round(b, 2)) == (1.0, 0.0, 0.0)


# ── vocab: color parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize("literal,expected", [
    ("#fff", "#ffffff"),
    ("#FFFA", "#ffffffaa"),
    ("#9b87f2", "#9b87f2"),
    ("#9b87f280", "#9b87f280"),
    ("rgb(155, 135, 242)", "#9b87f2"),
    ("rgba(255,255,255,.08)", "#ffffff14"),
    ("rgb(255 255 255 / 45%)", "#ffffff73"),
    ("rgb(100% 0% 0%)", "#ff0000"),
    ("hsl(0, 100%, 50%)", "#ff0000"),
    ("hsla(120deg 100% 25% / 0.5)", "#00800080"),
    ("oklch(0.62796 0.25768 29.2339)", "#ff0000"),
    ("color(srgb 1 0 0)", "#ff0000"),
    ("red", "#ff0000"),
    ("transparent", "#00000000"),
])
def test_parse_every_css_color_form(literal, expected):
    assert vocab.normalize_color(literal) == expected


@pytest.mark.parametrize("not_a_color", ["var(--pl-color-bg)", "currentColor", "inherit", "calc(1px)", "rgb(1,2)", "#12345", "banana", ""])
def test_non_literals_are_not_colors(not_a_color):
    assert vocab.parse_color(not_a_color) is None


def test_parse_length_px_rem_and_relatives():
    assert vocab.parse_length("12px") == 12
    assert vocab.parse_length("0.75rem") == 12
    assert vocab.parse_length("0") == 0
    assert vocab.parse_length("1em") is None and vocab.parse_length("50%") is None


# ── vocab: structure ──────────────────────────────────────────────────────────


def test_vocab_themes_are_structural(V):
    assert V.source == "css"
    assert V.value("--pl-color-accent", "dark") == "#9b87f2"
    assert V.value("--pl-color-accent", "light") == "#6366f1"
    assert V.value("--pl-space-2", "light") == "8px"          # base overlay, not a dropped token
    assert V.is_themed("--pl-color-accent") and not V.is_themed("--pl-space-2")
    assert V.prefix == "--pl-"


def test_reverse_map_and_exact_colors_prefer_role_tokens(V):
    assert V.vars_for_value("#6366f1")[0] == "--pl-color-accent"   # semantic before brand-*
    assert "--pl-color-brand-indigo" in V.vars_for_value("#6366f1")
    assert V.exact_colors("rgb(155 135 242)") == ["--pl-color-accent"]


def test_nearest_color_reports_delta_e_and_counts_alpha(V):
    name, d = V.nearest_color("#9a86f0")
    assert name == "--pl-color-accent" and d < 1
    # Same RGB as the border token, different alpha — alpha is part of the distance.
    far = V.color_matches("rgba(255,255,255,0.5)", 1)[0]
    assert far["alpha_delta"] > 0.4 and far["distance"] > 40
    # An oklch token matches its own computed color.
    assert V.nearest_color(vocab.normalize_color("oklch(0.64 0.16 25)"))[0] == "--pl-color-status-error"


def test_scales_and_missing_scales(V):
    assert [px for _, px in V.scale("space")] == [4, 8, 12, 16]
    assert V.nearest_length("space", "10px") == ("--pl-space-2", 8, 2)
    assert V.nearest_length("space", "0.75rem")[0] == "--pl-space-3"
    # One radius token and one font size are VALUES, not scales → DS gaps.
    assert set(V.missing_scales()) == {"radius", "font-size"}
    assert V.has_scale("shadow")


def test_json_only_vocab_uses_dotted_paths_not_invented_var_names():
    v = vocab.build_vocab("", json.dumps({"color": {"brand": {"lavender": "#9b87f2"}}}))
    assert v.source == "json" and v.exact_colors("#9b87f2") == ["color.brand.lavender"]
    assert v.ref("color.brand.lavender") == "color.brand.lavender"


def test_kit_custom_properties_become_known_names():
    extra = vocab.kit_custom_properties(".pl-shell { --pl-shell-rail: 48px; color: red } /* --pl-nope: 1 */")
    assert extra == {"--pl-shell-rail"}
    v = vocab.build_vocab(TOKENS_CSS, extra_vars=extra)
    assert v.is_known("--pl-shell-rail") and not v.is_known("--pl-nope")


# ── rules ─────────────────────────────────────────────────────────────────────


def test_raw_color_exact_near_and_far(V):
    f, _ = _run(".a { color: #9b87f2; background: #9580ee; border-color: #123456; }", "a.css", V)
    exact, near, far = [x for x in f if x["rule"] == "raw-color"]
    assert "exactly var(--pl-color-accent)" in exact["message"] and "won't follow the theme" in exact["message"]
    assert "≈ var(--pl-color-accent)" in near["message"] and 1 < near["delta_e"] < 5
    assert "not a design token" in far["message"]


def test_raw_color_negatives(V):
    code = """#abc { color: var(--pl-color-fg); fill: currentColor; background: transparent; }
.b { background: url("data:image/svg+xml,%23ff0000"); }
/* .c { color: #ff0000 } */"""
    f, _ = _run(code, "a.css", V)
    assert "raw-color" not in _rules(f)


def test_translucent_token_color_suggests_color_mix(V):
    f, _ = _run(".a { background: rgba(155, 135, 242, 0.12); }", "a.css", V)
    assert "color-mix(in srgb, var(--pl-color-accent) 12%, transparent)" in f[0]["suggestion"]


def test_raw_color_in_tsx_needs_a_color_context(V):
    code = 'const s = { color: "#9b87f2" };\nconst ref = "see #123 and #add";\n<a href="#fed">x</a>'
    f, _ = _run(code, "A.tsx", V)
    assert [x["line"] for x in f if x["rule"] == "raw-color"] == [1]


def test_svg_presentation_colors_are_info(V):
    f, _ = _run('<stop stopColor="#9b87f2" />\n<path fill="#9b87f2" />', "Logo.tsx", V)
    assert {x["severity"] for x in f if x["rule"] == "raw-color"} == {"info"}


def test_stale_fallback(V):
    f, _ = _run(""".a { color: var(--pl-color-accent, #7c8cff); }
.b { color: var(--pl-color-accent, #6366f1); }
.c { border-color: var(--pl-color-border, rgba(255,255,255,.08)); }
.d { color: var(--pl-color-accent, var(--brand)); }
.e { color: var(--pl-color-accent); }
.f { font-family: var(--pl-font-mono, monospace); }""", "a.css", V)
    stale = [x for x in f if x["rule"] == "stale-fallback"]
    assert [x["line"] for x in stale] == [1, 6]           # light value ok, rgba≡token ok, nested var skipped
    assert stale[0]["severity"] == "warn" and "#9b87f2" in stale[0]["suggestion"]
    assert stale[1]["severity"] == "info"                 # a keyword fallback is a degradation, not a stale color


def test_stale_fallback_matches_oklch_token_by_color_not_text(V):
    computed = vocab.normalize_color("oklch(0.64 0.16 25)")
    f, _ = _run(f".a {{ color: var(--pl-color-status-error, {computed}); }}", "a.css", V)
    assert "stale-fallback" not in _rules(f)


def test_unknown_token_suggests_close_names(V):
    f, _ = _run(".a { color: var(--pl-color-fgg); }", "a.css", V)
    assert f[0]["rule"] == "unknown-token" and f[0]["severity"] == "error"
    assert "--pl-color-fg" in f[0]["suggestion"]


def test_unknown_token_skips_names_the_app_defines(V):
    f, _ = _run(":root { --pl-app-rail: 48px; } .a { width: var(--pl-app-rail); }", "a.css", V)
    assert "unknown-token" not in _rules(f)


def test_off_scale_length_with_a_scale(V):
    f, _ = _run(".a { padding: 8px 10px; gap: 1px; margin: 0 auto; }", "a.css", V)
    off = [x for x in f if x["rule"] == "off-scale-length"]
    assert [(x["severity"], x["value_px"]) for x in off] == [("warn", 8), ("info", 10)]
    assert "var(--pl-space-2)" in off[0]["suggestion"]


def test_missing_scale_aggregates_to_one_ds_gap(V):
    code = "\n".join(f".t{i} {{ font-size: {11 + i % 3}px; }}" for i in range(9))
    f, ctx = _run(code, "a.css", V)
    assert "off-scale-length" not in _rules(f)           # no per-line spam when the DS has no scale
    gaps = [g for g in audit.aggregate(ctx, V) if g["rule"] == "missing-scale"]
    assert len(gaps) == 1 and gaps[0]["lane"] == "ds" and gaps[0]["count"] == 9
    assert "12px ×3" in gaps[0]["suggestion"]


def test_single_token_value_is_still_a_direct_hit(V):
    f, ctx = _run(".a { border-radius: 4px; } .b { border-radius: 999px; } .c { border-radius: 50%; }", "a.css", V)
    assert [x["message"] for x in f] == ["hardcoded border-radius: 4px is exactly var(--pl-radius)"]
    assert not ctx.missing


def test_box_shadow_owns_its_colors(V):
    f, _ = _run(".a { box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35); } .b { box-shadow: 0 2px 4px rgba(0,0,0,.2); }", "a.css", V)
    assert _rules(f) == ["off-scale-length", "off-scale-length"]
    assert "var(--pl-shadow-card)" in f[0]["suggestion"]


def test_ds_class_override_judges_the_selector_subject(V):
    f, _ = _run(""".x .pl-button { color: red; }
.pl-markdown pre { margin: 0; }
.x .pl-markdown [data-block] { margin: 0; }
.pl-message:not(.pl-message--user) .pl-message__content { min-width: 0; }
.wrap:has(.pl-tabs) { gap: 0; }
@media (max-width: 600px) { .y > .pl-dialog__body { padding: 0; } }""", "a.css", V, rules=["ds-class-override"])
    assert [(x["line"], x["classes"]) for x in f] == [(1, [".pl-button"]), (4, [".pl-message__content"]), (6, [".pl-dialog__body"])]


def test_ds_class_override_is_css_only(V):
    f, _ = _run('<div className="pl-button x">{".pl-button { }"}</div>', "A.tsx", V)
    assert "ds-class-override" not in _rules(f)


def test_legacy_alias_definition_and_references(V):
    f, _ = _run(""":root { --brand-indigo: #6366f1; --app-gap: 8px; --my-thing: #123456; }
.a { color: var(--pl-color-accent, var(--brand-indigo, #6366f1)); }
.b { color: var(--old-accent, #9b87f2); }""", "a.css", V)
    alias = [x for x in f if x["rule"] == "legacy-alias"]
    assert sorted(x["line"] for x in alias) == [1, 2, 3]
    alias.sort(key=lambda x: x["line"])
    assert "var(--pl-color-brand-indigo)" in alias[0]["suggestion"]  # the name it resembles, not accent
    assert "raw-color" not in [x["rule"] for x in f if x["line"] in (2, 3)]
    # A non-token definition is a raw color, not an alias.
    assert any(x["rule"] == "raw-color" and "#123456" in x["message"] for x in f)


def test_hand_rolled_controls(V):
    code = """<button onClick={go}>Go</button>
<input type="hidden" />
<input type="checkbox" />
<input value={v} />
<textarea />
<Button>ok</Button>"""
    f, _ = _run(code, "A.tsx", V, inventory=["Button", "Input", "Checkbox"])
    got = [(x["line"], x["component"]) for x in f if x["rule"] == "hand-rolled-control"]
    assert got == [(1, "Button"), (3, "Checkbox"), (4, "Input")]   # hidden skipped; no Textarea in the DS
    f2, _ = _run("<textarea />", "A.tsx", V)                       # no inventory → default map
    assert f2[0]["component"] == "Textarea"
    f3, _ = _run("const b = '<button>';", "a.css", V)
    assert "hand-rolled-control" not in _rules(f3)


def test_shadow_components(V):
    inv = ["StatusDot", "Chip", "Dialog", "Markdown"]
    code = """import { ConfirmDialog, Markdown as DSMarkdown } from "@protolabsai/ui/overlays";
export function StatusDot() { return null; }
const ModelChip = () => <span />;
function DeleteDialog() { return <ConfirmDialog />; }
export function Markdown() { return <DSMarkdown />; }
function helper() {}"""
    f, _ = _run(code, "A.tsx", V, inventory=inv)
    got = {x["local"]: x["severity"] for x in f if x["rule"] == "shadow-component"}
    assert got == {"StatusDot": "warn", "ModelChip": "info", "Markdown": "info"}  # DeleteDialog composes a DS dialog


def test_shadow_component_needs_an_inventory(V):
    f, _ = _run("export function StatusDot() { return null; }", "A.tsx", V)
    assert "shadow-component" not in _rules(f)


def test_foreign_ui_libs(V):
    code = """import { Box } from "@mui/material";
import * as Dialog from "@radix-ui/react-dialog";
import { Button } from "@/components/ui/button";
import x from "antd/es/button";
const y = require("react-bootstrap");
import { Button as Ok } from "@protolabsai/ui/primitives";
import "./local.css";"""
    f, _ = _run(code, "A.tsx", V)
    assert [x["package"] for x in f if x["rule"] == "foreign-ui-lib"] == ["@mui/material", "@radix-ui/react-dialog", "@/components/ui/button", "antd/es/button", "react-bootstrap"]
    f2, _ = _run('@import "bootstrap/dist/css/bootstrap.css";', "a.css", V)
    assert _rules(f2) == ["foreign-ui-lib"]


def test_suppression_forms(V):
    code = """.a { color: #ff0000; } /* ds-audit-ignore */
.b { color: #ff0000; font-size: 13px; } /* ds-audit-ignore raw-color */
/* ds-audit-ignore-next-line */
.c { color: #ff0000; }
.d { color: #ff0000; }"""
    f, _ = _run(code, "a.css", V)
    assert [x["line"] for x in f if x["rule"] == "raw-color"] == [5]
    f2, _ = _run("/* ds-audit-ignore-file */ .a { color: red }", "a.css", V)
    assert f2 == []


def test_rules_are_individually_toggleable(V):
    code = ".a { color: #ff0000; border: var(--pl-nope); }"
    only, _ = _run(code, "a.css", V, rules=["unknown-token"])
    assert _rules(only) == ["unknown-token"]
    assert set(audit.RULES) >= {"raw-color", "stale-fallback", "unknown-token", "off-scale-length", "ds-class-override",
                                "legacy-alias", "hand-rolled-control", "shadow-component", "foreign-ui-lib", "missing-scale"}


def test_findings_carry_the_contract_fields(V):
    f, _ = _run("\n.a {\n  color: #9b87f2;\n}", "x/a.css", V)
    assert set(f[0]) >= {"rule", "severity", "lane", "file", "line", "col", "snippet", "message", "suggestion", "group"}
    assert (f[0]["file"], f[0]["line"], f[0]["col"], f[0]["snippet"]) == ("x/a.css", 3, 10, "color: #9b87f2;")
    assert f[0]["lane"] == "consumer"


def test_token_file_and_minified_files_are_skipped(V):
    ctx = audit.AuditContext()
    assert audit.audit_text(TOKENS_CSS, "dist/tokens.css", V, ctx=ctx) == []
    assert audit.audit_text(".a{color:#f00}" * 400, "app.css", V, ctx=ctx) == []
    assert len(ctx.skipped) == 2


def test_exported_components_reads_the_real_inventory():
    src = """export function Button() {}
export const Input = forwardRef(() => null);
export { Dialog, Popover as Pop, useThing };
export const BUILTIN = {};
function Internal() {}"""
    assert audit.exported_components(src) == ["Button", "Dialog", "Input", "Pop"]


# ── walker, score, reports ────────────────────────────────────────────────────


def test_walker_skips_deps_build_tests_and_the_token_file():
    files, truncated = audit.walk(FIX / "app")
    rel = sorted(p.relative_to(FIX / "app").as_posix() for p in files)
    assert rel == ["src/app.css", "src/components/Widget.tsx", "src/ignored.css", "src/legacy.css"]
    assert not truncated
    only, _ = audit.walk(FIX / "app", include_globs="*.tsx")
    assert [p.name for p in only] == ["Widget.tsx"]
    none, _ = audit.walk(FIX / "app", exclude_globs="src/components")
    assert "Widget.tsx" not in [p.name for p in none]
    capped, truncated = audit.walk(FIX / "app", max_files=1)
    assert len(capped) == 1 and truncated


def test_score_formula():
    ctx = audit.AuditContext()
    ctx.token_refs, ctx.ds_imports = 7, 1
    fs = [{"severity": "error", "lane": "consumer"}, {"severity": "warn", "lane": "consumer"},
          {"severity": "info", "lane": "consumer"}, {"severity": "warn", "lane": "ds"}]
    # good 8 / (8 + 3 + 1 + 0.25) → 65; the ds-lane finding costs nothing.
    assert audit.score(fs, ctx) == 65
    assert audit.score([], audit.AuditContext()) == 100


def test_fixture_tree_smoke(V):
    """Golden-ish: one audit of the fixture tree, asserting the shape a report depends on."""
    res = audit.audit_tree(FIX / "app", V, inventory=["Button", "Input", "StatusDot", "Dialog"])
    s = res["summary"]
    assert s["files_scanned"] == 3 and any("ignored.css" in x for x in s["skipped"])
    assert s["by_rule"]["stale-fallback"] == 2
    assert s["by_rule"]["unknown-token"] == 1
    assert s["by_rule"]["legacy-alias"] == 3
    assert s["by_rule"]["ds-class-override"] == 3
    assert s["by_rule"]["foreign-ui-lib"] == 1
    assert s["by_rule"]["shadow-component"] == 1
    assert s["by_rule"]["hand-rolled-control"] == 1
    lanes = {f["rule"]: f["lane"] for f in res["findings"]}
    assert lanes["missing-scale"] == "ds" and lanes["override-hotspot"] == "ds" and lanes["raw-color"] == "consumer"
    assert 0 < s["score"] < 100
    # Nothing leaked in from node_modules / dist / tests / the token file.
    assert all(f["file"].startswith("src/") and ".test." not in f["file"] for f in res["findings"] if f["file"])


def test_markdown_groups_by_lane_and_theme_and_caps(V):
    code = "\n".join(f".a{i} {{ color: var(--pl-color-accent, #7c8cff); }}" for i in range(9))
    ctx = audit.AuditContext()
    fs = audit.audit_text(code + "\n.t { font-size: 13px; }", "a.css", V, ctx=ctx)
    allf, summary = audit.finalize(fs, ctx, V)
    md = audit.render_markdown(allf, summary, per_group=3, vocab=V)
    assert md.index("## Design-system lane") < md.index("## Consumer lane")
    assert "**--pl-color-accent ← #7c8cff** ×9" in md
    assert "`a.css:1`" in md and "+6 more" in md
    assert "Adherence score" in md and "no token scale for" in md
    j = json.loads(audit.render_json(allf, summary, root="/x", vocab=V))
    assert j["summary"]["by_rule"]["stale-fallback"] == 9 and len(j["findings"]) == 10


# ── tools ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("DESIGN_SYSTEM_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: TOKENS_CSS if path.endswith(".css") else "{}")
    monkeypatch.setattr(ds, "_inventory", lambda force=False: ["Button", "Input", "StatusDot"])
    monkeypatch.setattr(ds, "_VOCAB_CACHE", None)
    monkeypatch.setattr(ds, "_AUDIT_ROOTS", [])
    monkeypatch.setattr(ds, "_host_config", lambda: None)
    return ds


def test_ds_check_runs_the_engine(plugin):
    out = plugin.ds_check.invoke({"code": ".a { color: #9b87f2; border: 1px solid var(--pl-color-bg, #111); font-size: 13px; }"})
    assert "[raw-color·warn]" in out and "var(--pl-color-accent)" in out
    assert "[stale-fallback·warn]" in out
    assert "Design-system gaps" in out and "type (font-size)" in out


def test_ds_check_guesses_tsx_and_uses_the_inventory(plugin):
    out = plugin.ds_check.invoke({"code": "export function StatusDot() { return <button>x</button>; }"})
    assert "snippet.tsx" in out and "[shadow-component·warn]" in out and "<Button>" in out


def test_ds_audit_repo_refuses_paths_outside_the_allowed_roots(plugin):
    out = plugin.ds_audit_repo.invoke({"path": str(FIX / "app")})
    assert out.startswith("Refused:") and "audit_roots" in out and "onboard_project" in out


def test_ds_audit_repo_owner_repo_not_on_disk_points_at_onboard_project(plugin):
    out = plugin.ds_audit_repo.invoke({"path": "someorg/somerepo"})
    assert 'onboard_project("someorg/somerepo")' in out and "never clones" in out


def test_ds_audit_repo_resolves_a_registered_projects_slug(plugin, monkeypatch):
    cfg = types.SimpleNamespace(onboarding_root="", projects=[{"name": "app", "path": str(FIX / "app"), "github": "acme/app"}], filesystem_projects=[])
    monkeypatch.setattr(plugin, "_host_config", lambda: cfg)
    out = plugin.ds_audit_repo.invoke({"path": "https://github.com/acme/app"})
    assert "Adherence score" in out and "Full report:" in out


def test_ds_audit_repo_under_an_audit_root_writes_both_reports(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_AUDIT_ROOTS", [str(FIX)])
    out = plugin.ds_audit_repo.invoke({"path": str(FIX / "app"), "max_findings": 3})
    assert "## Design-system lane" in out and "## Consumer lane" in out
    assert "most severe of" in out                       # max_findings capped the reply…
    reports = sorted((tmp_path / "data" / "audits").iterdir())
    assert [p.suffix for p in reports] == [".json", ".md"]
    data = json.loads(reports[0].read_text())
    assert len(data["findings"]) > 3                      # …but not the report
    assert data["vocab"]["missing_scales"] == ["radius", "font-size"]


def test_ds_audit_repo_onboarding_root_counts_and_escape_is_refused(plugin, monkeypatch):
    cfg = types.SimpleNamespace(onboarding_root=str(FIX / "app" / "src"), projects=[], filesystem_projects=[])
    monkeypatch.setattr(plugin, "_host_config", lambda: cfg)
    assert "Adherence score" in plugin.ds_audit_repo.invoke({"path": str(FIX / "app" / "src" / "components")})
    # `..` resolves out of the root before the check.
    assert plugin.ds_audit_repo.invoke({"path": str(FIX / "app" / "src" / ".." / "..")}).startswith("Refused:")


def test_ds_audit_repo_rejects_unknown_rules(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_AUDIT_ROOTS", [str(FIX)])
    out = plugin.ds_audit_repo.invoke({"path": str(FIX / "app"), "rules": "raw-color,nope"})
    assert "unknown rule id(s) nope" in out


def test_ds_audit_repo_rules_filter(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_AUDIT_ROOTS", [str(FIX)])
    out = plugin.ds_audit_repo.invoke({"path": str(FIX / "app"), "rules": "unknown-token"})
    assert "`unknown-token`" in out and "`raw-color`" not in out


def test_audit_roots_setting_parses_lists_and_strings():
    assert ds._parse_roots(["~/a", " ", "/b"]) == ["~/a", "/b"]
    assert ds._parse_roots("~/a, /b\n/c") == ["~/a", "/b", "/c"]
    assert ds._parse_roots(None) == []


def test_ds_audit_repo_is_registered_and_documented():
    import yaml

    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert "audit_roots" in manifest["config"]
    assert any(s["key"] == "audit_roots" for s in manifest["settings"])
    readme = (ROOT / "README.md").read_text()
    assert "ds_audit_repo" in readme
    skill = (ROOT / "skills" / "auditing-a-repo" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: auditing-a-repo\n")
    for step in ("onboard_project", "ds_audit_repo", "## Gap", "## Evidence", "## Proposed API", "## Priority", "## Context"):
        assert step in skill, step
    src = (ROOT / "__init__.py").read_text()
    assert "ds_audit_repo," in src.split("registry.register_tools(")[1].split(")")[0]
