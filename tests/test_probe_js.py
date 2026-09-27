"""The in-page site probe (siteprobe_js.py) — shipped separately from its analyzer so each PR
stays reviewable. SITE_PROBE_JS is the minified build of SITE_PROBE_SOURCE (scripts/build_probe.py);
these tests pin that the shipped build matches its source and that it fits a Windows command line
(browser_eval passes the script as one argv element)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("ds_test_probe_js", ROOT / "siteprobe_js.py")
js_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(js_mod)


def _script(prefix: str = "pl", cap: int = 30000) -> str:
    return js_mod.SITE_PROBE_JS.replace("__DS_PREFIX__", prefix).replace("__CAP__", str(cap))


def test_the_shipped_script_is_the_minified_build_of_the_source():
    """Editing SITE_PROBE_SOURCE without re-running scripts/build_probe.py would ship stale code."""
    assert hashlib.sha256(js_mod.SITE_PROBE_SOURCE.encode()).hexdigest() == js_mod.SOURCE_SHA256, "run scripts/build_probe.py"


def test_the_script_is_one_expression_with_both_placeholders():
    js = js_mod.SITE_PROBE_JS
    assert js.startswith("(()=>{") and js.rstrip().endswith("})()")
    assert "__DS_PREFIX__" in js and "__CAP__" in js
    filled = _script("pl", 12345)
    assert "__DS_PREFIX__" not in filled and "__CAP__" not in filled


def test_the_probe_fits_a_windows_command_line():
    """CreateProcess caps a whole command line at 32,767 chars. Budget: the script (quoted the way
    Windows quotes it) stays <= 24 KiB, and a worst-case realistic command line keeps >= 6,000 chars
    of headroom."""
    js = _script("pl")
    quoted = subprocess.list2cmdline([js])
    assert len(quoted) <= 24 * 1024, len(quoted)
    exe = "C:\\Users\\a-rather-long-user-name\\AppData\\Local\\protoAgent\\box\\cache\\agent-browser\\0.26.0\\agent-browser-win32-x64.exe"
    flags = ["--headed", "--profile", "C:/Users/a-rather-long-user-name/AppData/Local/protoAgent/agent_browser/profiles/default",
             "--device", "iPhone 15 Pro Max", "--allowed-domains", "example.com,*.example.org,docs.example.net,*.cdn.example.com",
             "--confirm-actions", "click,fill", "--max-output", "200000", "--user-agent", "M" * 200,
             "--args", ("--lang=en-US,--window-size=1440,900,--disable-backgrounding-occluded-windows,--disable-renderer-backgrounding,"
                        "--disable-background-timer-throttling,--disable-blink-features=AutomationControlled")]
    assert len(subprocess.list2cmdline([exe, *flags, "eval", js])) <= 32_767 - 6_000


# ── the probe in a real browser (skipped without Playwright + Chromium) ─────────

FIX = Path(__file__).resolve().parent / "fixtures"


def _browser():
    pw = pytest.importorskip("playwright.sync_api")
    ctx = pw.sync_playwright().start()
    import glob

    exes = [None] + sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))[::-1]
    for exe in exes:
        try:
            return ctx, ctx.chromium.launch(executable_path=exe) if exe else ctx.chromium.launch()
        except Exception:  # noqa: BLE001, S112 — not installed there: try the next install
            continue
    ctx.stop()
    pytest.skip("no Chromium for Playwright on this machine")


@pytest.fixture(scope="module")
def page():
    ctx, browser = _browser()
    pg = browser.new_page(viewport={"width": 1280, "height": 800})
    yield pg
    browser.close()
    ctx.stop()


def test_probe_runs_in_a_real_browser_on_a_ds_page(page):
    page.goto((FIX / "sites" / "ds_app.html").as_uri())
    out = page.evaluate(_script("pl", 30000))
    p = json.loads(out)
    assert p["probe"] == "ds-site-probe" and len(out) < 30000
    assert p["vars"]["ds"] >= 10 and p["dsClasses"]["total"] > 0
    kinds = {c["kind"] for c in p["clusters"]}
    assert {"button", "card", "badge", "tab", "breadcrumb-item", "pagination-item", "icon-button", "input"} <= kinds


def test_probe_hard_caps_its_output_on_a_huge_page(page):
    rows = "".join(
        f'<div class="row-{i % 97} tone-{i % 13}" style="padding:{i % 40}px;margin:{i % 23}px;color:rgb({i % 255},{(i * 7) % 255},{(i * 13) % 255});'
        f'background:rgb({(i * 3) % 255},{(i * 5) % 255},{(i * 11) % 255});border-radius:{i % 17}px;font-size:{8 + i % 30}px">'
        f'<span class="label-{i % 53}">Item {i} with some text</span><button class="btn-{i % 41}">Go {i}</button></div>'
        for i in range(6000)
    )
    page.set_content(f"<html><body>{rows}</body></html>")
    out = page.evaluate(_script("pl", 12000))
    p = json.loads(out)
    assert len(out) <= 12000
    assert p["truncated"] and any(t.startswith(("walk:", "shrink:")) for t in p["truncated"])
    assert p["walked"] <= 5000
