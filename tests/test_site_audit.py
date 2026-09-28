"""Site audit — the in-page probe (siteprobe_js.py), the analyzer (siteprobe.py), the guarded
fetcher (fetch.py) and the three tools on top (ds_site_probe_script, ds_audit_url,
ds_component_gaps). Host-free and network-free.

The probe fixtures under tests/fixtures/probes/ are REAL probe output: SITE_PROBE_JS run in
headless Chromium against the two pages in tests/fixtures/sites/ (the URLs were then replaced
with example.* hosts). To regenerate after changing the probe, load each page with Playwright,
``page.evaluate(siteprobe.SITE_PROBE_JS)`` and save the returned string. The last tests here do
exactly that when Playwright + a Chromium are available, and skip otherwise.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sp = _load("ds_test_siteprobe", "siteprobe.py")
ds = _load("ds_test_plugin_site", "__init__.py")

TOKENS_CSS = (FIX / "ds" / "tokens.css").read_text()
DS_PROBE = (FIX / "probes" / "ds_app.json").read_text()
FOREIGN_PROBE = (FIX / "probes" / "foreign_marketing.json").read_text()
INVENTORY = ["Button", "Card", "Badge", "Tabs", "Input", "Row", "Avatar", "Heading", "Header", "Navigation", "TextLink", "ToastProvider", "Hero", "FormField"]
STORYBOOK = [
    {"label": "Button", "title": "Components/Primitives/Button", "stories": [{"name": "Default"}, {"name": "Primary"}]},
    {"label": "Badge", "title": "Components/Primitives/Badge", "stories": [{"name": "Neutral"}, {"name": "Success"}]},
    {"label": "Forms", "title": "Components/Forms", "stories": [{"name": "Inputs"}]},
]


@pytest.fixture(scope="module")
def V():
    return sp.vocab_mod.build_vocab(TOKENS_CSS)


def _probe(text=DS_PROBE):
    return sp.merge_probes(sp.parse_probe(text))


def _rules(findings):
    return {f["rule"] for f in findings}


# ── the script ────────────────────────────────────────────────────────────────


def test_probe_script_is_one_expression_with_every_placeholder_filled():
    js = sp.probe_script("pl", cap=12345)
    assert js.startswith("(()=>{") and js.rstrip().endswith("})()")
    assert "__DS_PREFIX__" not in js and "__CAP__" not in js
    assert "='pl'" in js and "+'12345'" in js
    assert "='acme'" in sp.probe_script("--acme-")  # the var prefix form works too
    assert sp.SITE_PROBE_JS == sp.probe_script()








def test_probe_fixtures_are_real_probe_output_under_the_cap():
    for text in (DS_PROBE, FOREIGN_PROBE):
        assert len(text) < sp.PROBE_CAP
        p = json.loads(text)
        assert p["probe"] == "ds-site-probe" and p["v"] == 1
        assert {"styles", "vars", "dsClasses", "landmarks", "forms", "clusters"} <= set(p)
        for c in p["clusters"]:
            assert len(c["html"]) <= 600
            assert {"kind", "n", "sig", "sel", "w", "h"} <= set(c)


# ── parsing / merging ─────────────────────────────────────────────────────────


def test_parse_probe_accepts_every_shape_browser_eval_and_scripts_produce():
    raw = DS_PROBE
    as_eval = json.dumps(raw)  # browser_eval prints a returned string JSON-encoded
    assert sp.parse_probe(raw)[0]["url"] == "https://acme.example/runs/42"
    assert sp.parse_probe(as_eval)[0]["url"] == "https://acme.example/runs/42"
    assert sp.parse_probe("✓ Done\n" + as_eval)[0]["probe"] == "ds-site-probe"
    assert sp.parse_probe("```json\n" + raw + "\n```")[0]["probe"] == "ds-site-probe"
    many = sp.parse_probe(json.dumps([json.dumps(raw), json.loads(FOREIGN_PROBE)]))
    assert [p["url"] for p in many] == ["https://acme.example/runs/42", "https://brightside.example/"]


@pytest.mark.parametrize("bad", ["", "not json", '{"hello": 1}', "[1, 2]", '"just a string"'])
def test_parse_probe_rejects_what_is_not_a_probe(bad):
    with pytest.raises(ValueError):
        sp.parse_probe(bad)


def test_merge_probes_sums_styles_and_merges_clusters_across_pages():
    one = json.loads(DS_PROBE)
    two = json.loads(DS_PROBE)
    two["url"] = "https://acme.example/runs/43"
    merged = sp.merge_probes([one, two])
    assert merged["pages"] == ["https://acme.example/runs/42", "https://acme.example/runs/43"]
    btn = next(c for c in merged["clusters"] if c["kind"] == "button")
    single = next(c for c in one["clusters"] if c["kind"] == "button")
    assert btn["n"] == 2 * single["n"] and len(btn["pages"]) == 2
    c0 = one["styles"]["colors"][0]
    m0 = next(r for r in merged["styles"]["colors"] if r["v"] == c0["v"])
    assert m0["n"] == 2 * c0["n"]
    assert sp.merge_probes([one])["pages"] == ["https://acme.example/runs/42"]


# ── the rendered audit ────────────────────────────────────────────────────────


def test_audit_probe_on_a_ds_consumer(V):
    findings, stats = sp.audit_probe(_probe(), V)
    rules = _rules(findings)
    assert "no-ds-adoption" not in rules and stats["adopted"]
    drift = [f for f in findings if f["rule"] == "token-value-drift"]
    assert [f["group"] for f in drift] == ["--pl-color-accent"]  # #8f7ae8 vs the DS's #9b87f2 / #6366f1
    unknown = {f["group"] for f in findings if f["rule"] == "unknown-token"}
    assert "--pl-color-brandish" in unknown
    lc = [f for f in findings if f["rule"] == "low-contrast"]
    assert lc and lc[0]["severity"] == "error" and lc[0]["ratio"] < 1.5  # #777 on #888
    unnamed = {f["message"].split("`")[1]: f for f in findings if f["rule"] == "unnamed-control"}
    assert set(unnamed) == {"icon-button", "input"}
    assert "non-semantic-control" in rules
    # The fixture DS has no type scale and a single radius → one ds-lane finding each, not one per value.
    ms = [f for f in findings if f["rule"] == "missing-scale"]
    assert sorted(f["group"] for f in ms) == ["missing-scale: font-size", "missing-scale: radius"] and all(f["lane"] == "ds" for f in ms)
    # every finding carries the audit.py shape
    for f in findings:
        assert {"rule", "severity", "lane", "file", "line", "col", "snippet", "message", "suggestion", "group"} <= set(f)


def test_audit_probe_on_a_site_that_does_not_use_the_ds(V):
    findings, stats = sp.audit_probe(_probe(FOREIGN_PROBE), V)
    rules = _rules(findings)
    assert "no-ds-adoption" in rules and not stats["adopted"]
    assert "palette-gap" not in rules  # no DS on the page → its colors are distance, not DS gaps
    assert "off-token-color" in rules and "low-contrast" in rules
    summary = sp.summarize_url(findings, stats)
    assert 0 <= summary["score"] < 50 and summary["mode"] == "rendered"


def test_audit_probe_rules_filter(V):
    findings, _ = sp.audit_probe(_probe(), V, rules=["low-contrast"])
    assert _rules(findings) == {"low-contrast"}


def test_contrast_and_background_compositing():
    assert sp.contrast_ratio("rgb(0, 0, 0)", ["rgb(255, 255, 255)"]) == 21.0
    assert sp.contrast_ratio("rgb(0, 0, 0)", ["image"]) is None
    # 50% black over white = mid grey under black text
    mid = sp.contrast_ratio("rgb(0, 0, 0)", ["rgba(0, 0, 0, 0.5)", "rgb(255, 255, 255)"])
    assert 5 < mid < 6
    # canvas follows the page's color scheme
    assert sp.contrast_ratio("rgb(255, 255, 255)", ["canvas"], dark=True) > 15


def test_token_values_compare_through_serialization_noise(V):
    assert sp._same_token_value(V, "--pl-space-2", "8px")
    assert sp._canon(".2s") == sp._canon("200ms")
    assert sp._canon("rgba(255,255,255,.08)") == sp._canon("rgba(255, 255, 255, 0.08)")
    assert sp._shadow_key("rgba(0, 0, 0, 0.35) 0px 1px 3px 0px") == sp._shadow_key("0 1px 3px rgba(0,0,0,.35)")
    assert sp._vocab_shadow(V, "rgba(0, 0, 0, 0.35) 0px 1px 3px 0px") == "--pl-shadow-card"


def test_rendered_report_renders_through_the_shared_markdown_renderer(V):
    probe = _probe()
    findings, stats = sp.audit_probe(probe, V)
    summary = sp.summarize_url(findings, stats)
    md = sp.audit.render_markdown(findings, summary, title="t", vocab=V, registry=sp.REPORT_RULES, stats_line=sp.stats_line_rendered(summary, probe))
    assert "rendered read of 1 page(s)" in md
    assert "### `low-contrast`" in md and "### `token-value-drift`" in md
    assert "https://acme.example/runs/42:0" not in md  # no fake line numbers on a URL


# ── component gaps ────────────────────────────────────────────────────────────


def _gaps(text=DS_PROBE, inventory=INVENTORY, sb=STORYBOOK, V=None):
    return sp.component_gaps(_probe(text), inventory, sb, V)


def test_component_gaps_classifies_a_ds_consumer(V):
    res = _gaps(V=V)
    by = {(e["status"], e["proposed_name"]): e for e in res["entries"]}
    assert ("MISSING", "Breadcrumb") in by and ("MISSING", "Pagination") in by
    pag = by[("MISSING", "Pagination")]
    assert (pag["count"], pag["items"]) == (1, 4)  # one pagination of 4 items; li > a inside each li isn't double-counted
    btn = next(e for e in res["entries"] if e["kind"] == "button")
    assert btn["status"] == "VARIANT GAP" and btn["component"] == "Button"
    assert any(m.startswith("sizes") for m in btn["missing_variants"]) and "a disabled state" in btn["missing_variants"]
    assert "size?" in btn["proposed_api"] and "disabled?: boolean" in btn["proposed_api"]
    assert ("VARIANT GAP", "Button") in by and any(e["kind"] == "icon-button" and "an icon-only variant" in e["missing_variants"] for e in res["entries"])
    assert any(e["status"] == "VARIANT GAP" and e["proposed_name"] == "Badge" for e in res["entries"])  # pill AND square badges
    covered = {e["component"] for e in res["entries"] if e["status"] == "COVERED"}
    assert {"Card", "Tabs", "Input"} <= covered
    inp = next(e for e in res["entries"] if e.get("component") == "Input")
    assert "unverified" in inp["reason"]  # only a group story ("Forms/Inputs") shows it — no variant claims
    order = [e["status"] for e in res["entries"]]
    assert order == sorted(order, key=["MISSING", "VARIANT GAP", "UNCLASSIFIED", "COVERED"].index)


def test_component_gaps_on_a_marketing_site(V):
    res = _gaps(FOREIGN_PROBE, V=V)
    names = {(e["status"], e["proposed_name"]) for e in res["entries"]}
    assert ("MISSING", "Carousel") in names and ("MISSING", "Footer") in names
    assert ("COVERED", "Button") in names  # .btn links → Button (as a link)
    link_btn = next(e for e in res["entries"] if e["kind"] == "link-button")
    assert "LINKS styled as buttons" in link_btn["reason"]
    assert any(e["status"] == "UNCLASSIFIED" for e in res["entries"])  # the logo tiles: look at them


def test_component_gaps_without_an_inventory_reads_everything_as_missing(V):
    res = _gaps(inventory=[], sb=None, V=V)
    assert {e["status"] for e in res["entries"]} <= {"MISSING", "UNCLASSIFIED"}


def test_gap_issues_use_the_established_format(V):
    res = _gaps(V=V)
    md = sp.render_gaps_markdown(res, ds_repo="acme/ds", report_path="/tmp/r.md")
    assert "## Missing — candidate NEW components" in md and "## Covered" in md and "## Ready-to-file gap issues" in md
    issue = sp.gap_issue(next(e for e in res["entries"] if e["status"] == "MISSING"), res, "acme/ds")
    for h in ("## Gap", "## Evidence", "## Proposed API", "## Priority", "## Context"):
        assert h in issue
    assert issue.index("## Gap") < issue.index("## Evidence") < issue.index("## Proposed API") < issue.index("## Priority") < issue.index("## Context")
    assert "```html" in issue and "acme/ds" in issue


# ── static mode ───────────────────────────────────────────────────────────────

PAGE_HTML = """<html><head><link rel="stylesheet" href="/a.css"><link rel="stylesheet" href="https://cdn.other.net/b.css">
<style>.x{color:#9b87f2}</style></head><body><div class="hero">hi</div></body></html>"""
SHEET_A = ".btn{background:#9a86f0;padding:16px;border-radius:6px}.card{color:var(--pl-color-fg, #cccccc)}" * 3


def test_unminify_keeps_strings_and_parens_intact():
    css = '.a{background:url("data:image/svg+xml;utf8,<svg>;</svg>");color:red}.b{content:";{}"}'
    out = sp.unminify_css(css)
    assert 'url("data:image/svg+xml;utf8,<svg>;</svg>");\n' in out and 'content:";{}"' in out
    assert out.count("\n") == 5


def test_static_audit_runs_the_css_rules_over_fetched_sheets(V):
    sheets = [("https://acme.example/a.css", SHEET_A), ("https://acme.example/tokens.css", ".z{color:#ff0000}")]
    findings, summary = sp.static_audit(PAGE_HTML, "https://acme.example/", sheets, V)
    files = {f["file"] for f in findings if f["lane"] == "consumer"}
    assert "https://acme.example/a.css" in files
    assert any(f.endswith("tokens.css#sheet.css") for f in files)  # a SITE's tokens.css is audited, not skipped as "the DS"
    assert {"raw-color", "stale-fallback", "off-scale-length"} <= _rules(findings)
    assert summary["mode"] == "static" and "no-ds-adoption" not in _rules(findings)  # it references --pl-*
    bare, _ = sp.static_audit("<html><body></body></html>", "https://x.example/", [("https://x.example/s.css", ".a{color:#123456}")], V)
    assert "no-ds-adoption" in _rules(bare)


def test_site_assets_and_stylesheet_policy():
    a = sp.site_assets(PAGE_HTML, "https://acme.example/page")
    assert a["stylesheets"] == ["https://acme.example/a.css", "https://cdn.other.net/b.css"]
    assert sp.stylesheet_allowed("https://cdn.acme.example/x.css", "https://www.acme.example/")
    assert not sp.stylesheet_allowed("https://cdn.other.net/b.css", "https://acme.example/")
    assert not sp.stylesheet_allowed("file:///etc/passwd", "https://acme.example/")


# ── the tools ─────────────────────────────────────────────────────────────────


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "_gh_get_raw", lambda path: TOKENS_CSS if path.endswith(".css") else "{}")
    monkeypatch.setattr(ds, "_inventory", lambda force=False: list(INVENTORY))
    monkeypatch.setattr(ds, "_sb_components", lambda force=False: STORYBOOK)
    monkeypatch.setattr(ds, "_VOCAB_CACHE", None)
    monkeypatch.setenv("DESIGN_SYSTEM_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)

    def no_network(*a, **k):
        raise AssertionError("tests never touch the network")

    monkeypatch.setattr(ds, "_url_fetch", no_network)
    return ds


def test_ds_site_probe_script(plugin):
    out = plugin.ds_site_probe_script.invoke({})
    assert "browser_open" in out and "browser_eval" in out and "ds_audit_url" in out and "execute_code" in out
    js = plugin.ds_site_probe_script.invoke({"script_only": True})
    assert js.startswith("(()=>{") and "='pl'" in js


def test_ds_audit_url_on_a_probe_writes_both_reports(plugin, tmp_path):
    out = plugin.ds_audit_url.invoke({"url_or_probe": json.dumps(DS_PROBE)})  # as browser_eval returns it
    assert "Adherence score" in out and "rendered read" in out and "low-contrast" in out
    md = sorted((tmp_path / "audits").glob("*.md"))
    js = sorted((tmp_path / "audits").glob("*.json"))
    assert md and js and "acme.example" in md[0].name
    data = json.loads(js[0].read_text())
    assert data["summary"]["mode"] == "rendered" and "low-contrast" in data["rules"]
    assert str(md[0]) in out


@pytest.mark.parametrize("visible", [0, 4])
def test_an_empty_probe_gets_no_verdict_not_a_perfect_score(plugin, tmp_path, visible):
    """#27: a probe taken before a Storybook story mounted (0 visible elements) scored
    100/100. Too little on the page to judge → refused, no score, no report written."""
    empty = dict(json.loads(DS_PROBE) if isinstance(DS_PROBE, str) else DS_PROBE, visible=visible)
    out = plugin.ds_audit_url.invoke({"url_or_probe": json.dumps(empty)})
    assert "no verdict" in out and "Adherence score" not in out and "hadn't rendered" in out
    assert not list((tmp_path / "audits").glob("*.md"))
    gaps = plugin.ds_component_gaps.invoke({"probe": json.dumps(empty)})
    assert "no verdict" in gaps


def test_ds_audit_url_merges_several_pages(plugin):
    both = json.dumps([json.dumps(DS_PROBE), json.dumps(FOREIGN_PROBE)])
    out = plugin.ds_audit_url.invoke({"url_or_probe": both})
    assert "rendered read of 2 page(s)" in out and "+1 pages" in out


def test_ds_audit_url_static_mode_with_the_fetch_seam(plugin, monkeypatch):
    calls = []

    def fake_fetch(url, max_bytes, deadline, allow_offsite_hosts=False):
        calls.append((url, allow_offsite_hosts))
        if url.endswith("/a.css"):
            return url, SHEET_A
        if url.endswith("/b.css"):
            raise RuntimeError("refusing to fetch cdn.other.net — it resolves to a non-public address (10.0.0.1)")
        return "https://acme.example/", PAGE_HTML

    monkeypatch.setattr(plugin, "_url_fetch", fake_fetch)
    out = plugin.ds_audit_url.invoke({"url_or_probe": "acme.example"})
    assert calls[0][0] == "https://acme.example"
    assert "STATIC" in out and "run the probe" in out.lower()
    assert "raw-color" in out and "1 failed" in out
    assert ("https://acme.example/a.css", True) in calls


def test_ds_audit_url_errors_are_legible(plugin, monkeypatch):
    assert "pass the probe JSON" in plugin.ds_audit_url.invoke({"url_or_probe": ""})
    assert "unknown rule" in plugin.ds_audit_url.invoke({"url_or_probe": DS_PROBE, "rules": "nope"})
    assert "not ds-site-probe output" in plugin.ds_audit_url.invoke({"url_or_probe": '{"a": 1}'})

    def refuse(*a, **k):
        raise RuntimeError("refusing to fetch 10.0.0.1 — it resolves to a non-public address (10.0.0.1)")

    monkeypatch.setattr(plugin, "_url_fetch", refuse)
    out = plugin.ds_audit_url.invoke({"url_or_probe": "http://10.0.0.1/"})
    assert "non-public" in out and "ds_site_probe_script" in out


def test_ds_component_gaps_tool(plugin, tmp_path):
    out = plugin.ds_component_gaps.invoke({"probe": json.dumps(DS_PROBE)})
    assert "## Missing — candidate NEW components" in out and "Breadcrumb" in out and "## Gap" in out
    assert sorted((tmp_path / "gaps").glob("*.json"))
    assert "not ds-site-probe output" in plugin.ds_component_gaps.invoke({"probe": "[]"})


def test_tools_are_registered_documented_and_declared():
    import yaml

    src = (ROOT / "__init__.py").read_text()
    reg = src.split("def register(")[1].split("registry.register_tools(")[0]
    for name in ("ds_site_probe_script", "ds_audit_url", "ds_component_gaps"):
        assert f"{name}," in reg, name
    readme = (ROOT / "README.md").read_text()
    for name in ("ds_site_probe_script", "ds_audit_url", "ds_component_gaps", "execute_code"):
        assert name in readme, name
    skill = (ROOT / "skills" / "auditing-a-site" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: auditing-a-site\n")
    for step in ("browser_open", "browser_eval", "ds_site_probe_script", "ds_audit_url", "ds_component_gaps", "browser_screenshot",
                 "execute_code", "show_artifact", "protoEngineer", "## Gap", "## Context"):
        assert step in skill, step
    index = (ROOT / "skills" / "using-the-design-system" / "SKILL.md").read_text()
    assert "auditing-a-site" in index and "auditing-a-repo" in index
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert "operator-supplied-urls" in manifest["capabilities"]["network"]  # a token, explained in a manifest comment
