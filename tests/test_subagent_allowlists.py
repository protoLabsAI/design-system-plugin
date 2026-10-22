"""Pins the ds-designer / design-critic / ds-explainer tool allowlists (#39).

A subagent is bound ONLY the tools its allowlist names, so a missing name is a capability the
subagent silently lacks, and an extra one is a capability it silently gains. Both directions
are pinned exactly here: widening or narrowing an allowlist is a deliberate change that must
edit this file too. Cross-plugin names must be READ tools — except the designer's own
render/check (`show_artifact`/`check_artifact`) plus `update_artifact`, its one opt-in write
(protoAgent#4327: write tools are opt-in per subagent spec, never inherited). The critic and
explainer have no write tool; nothing here is a comment or merge."""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("design_system_plugin_allow", _ROOT / "__init__.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


@pytest.fixture(autouse=True)
def _stub_subagent_config(monkeypatch):
    stub = types.ModuleType("graph.subagents.config")

    class SubagentConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    stub.SubagentConfig = SubagentConfig
    monkeypatch.setitem(sys.modules, "graph.subagents.config", stub)


DESIGNER = [
    "ds_rules", "ds_kit_classes", "ds_tokens", "ds_stories", "ds_story", "ds_components", "ds_component",
    "ds_check", "theme_contrast",
    "show_artifact", "check_artifact", "update_artifact",
    "get_artifact", "list_artifacts",
    "github_get_issue", "github_read_file",
    "browser_open", "browser_snapshot", "browser_screenshot",
]
CRITIC = ["ds_rules", "ds_tokens", "ds_check", "ds_components", "ds_component", "theme_contrast", "get_artifact"]
EXPLAINER = ["ds_rules", "ds_tokens", "ds_components", "ds_component", "ds_stories", "ds_story", "ds_check"]

# Verbs that mark a tool as changing state somewhere. None may reach a subagent allowlist
# except the designer's render/check of its own prototype plus its one opt-in write,
# update_artifact (protoAgent#4327: write tools are opt-in per subagent spec, never inherited).
_WRITE = re.compile(
    r"(^|_)(update|rewrite|delete|create|edit|merge|close|comment|reply|resolve|approve|review|"
    r"set|apply|write|push|click|fill|eval|drift|refresh)(_|$)"
)
_DESIGNER_ALLOWED_WRITES = {"show_artifact", "check_artifact", "update_artifact"}


def test_designer_allowlist_is_pinned():
    assert ds._build_designer().tools == DESIGNER


def test_critic_allowlist_is_pinned():
    assert ds._build_design_critic().tools == CRITIC


def test_explainer_allowlist_is_pinned():
    assert ds._build_explainer().tools == EXPLAINER


def test_issue_39_acceptance():
    designer = set(ds._build_designer().tools)
    assert {"ds_component", "theme_contrast"} <= designer
    assert {"get_artifact", "github_get_issue", "github_read_file", "browser_open", "browser_snapshot", "browser_screenshot"} <= designer
    assert "get_artifact" in ds._build_design_critic().tools


@pytest.mark.parametrize("build", ["_build_designer", "_build_design_critic", "_build_explainer"])
def test_no_write_or_merge_tools(build):
    tools = getattr(ds, build)().tools
    assert len(tools) == len(set(tools)), "duplicate allowlist entry"
    allowed = _DESIGNER_ALLOWED_WRITES if build == "_build_designer" else set()
    writes = [t for t in tools if _WRITE.search(t) and t not in allowed]
    assert not writes, writes
    # update_artifact is the designer's one opt-in write; the critic and explainer stay
    # read-only, so it must not reach either of them. github_merge_pr reaches none of the three.
    if build != "_build_designer":
        assert "update_artifact" not in tools
    assert "github_merge_pr" not in tools


def test_4296_designer_opts_into_update_artifact_critic_stays_read_only():
    """#4296 edit half: ds-designer names update_artifact (its one opt-in write under the
    protoAgent#4327 rule); the critic and explainer stay read-only and get no write tool."""
    assert "update_artifact" in ds._build_designer().tools
    critic = ds._build_design_critic().tools
    assert not [t for t in critic if _WRITE.search(t)]
    assert "update_artifact" not in critic and "rewrite_artifact" not in critic
    explainer = ds._build_explainer().tools
    assert not [t for t in explainer if _WRITE.search(t)]


def test_this_plugins_names_resolve_to_its_own_tools():
    """Every allowlisted name that isn't another plugin's must be a tool this plugin defines,
    so a rename here can't leave a dead name behind."""
    own = {getattr(ds, n).name for n in dir(ds) if hasattr(getattr(ds, n), "invoke") and hasattr(getattr(ds, n), "name")}
    external = set(ds.DESIGNER_EXTERNAL_TOOLS) | set(ds.CRITIC_EXTERNAL_TOOLS)
    for build in ("_build_designer", "_build_design_critic", "_build_explainer"):
        for t in getattr(ds, build)().tools:
            assert t in own or t in external, (build, t)
    assert not (external & own), "an external name shadows one of this plugin's tools"


def test_prompts_say_when_to_use_the_new_tools():
    for t in ("ds_component", "theme_contrast", "github_get_issue", "github_read_file", "get_artifact", "update_artifact", "browser_screenshot"):
        assert t in ds._DESIGNER_PROMPT, t
    assert "get_artifact" in ds._CRITIC_PROMPT and "theme_contrast" in ds._CRITIC_PROMPT
    # the browser tools read existing pages; the designer's own render is checked by check_artifact
    assert "not your artifact" in ds._DESIGNER_PROMPT and "check_artifact" in ds._DESIGNER_PROMPT
