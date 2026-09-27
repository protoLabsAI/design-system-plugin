"""The in-page site probe (siteprobe_js.py) — shipped separately from its analyzer so each PR
stays reviewable. SITE_PROBE_JS is the minified build of SITE_PROBE_SOURCE (scripts/build_probe.py);
these tests pin that the shipped build matches its source and that it fits a Windows command line
(browser_eval passes the script as one argv element)."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

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
