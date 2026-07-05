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
