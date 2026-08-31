"""design-system plugin (ADR 0027) — the agent's LIVE window into a
design system.

The design system is a live source of truth: ``@protolabsai/design`` owns the brand values
(``src/tokens.js`` → built ``dist/tokens.json`` → ``--pl-*`` CSS vars + a Tailwind preset),
``packages/ui`` owns the components, and ``docs/reference/visual-identity.md`` owns the rules.
This plugin reads them STRAIGHT FROM the repo at call time so the agent works from the current
vocabulary — never a stale copy frozen into its knowledge base (the anti-drift principle).

Tools:
  ds_tokens      — the live token vocabulary (colors, spacing, radius, type, …)
  ds_components  — the component inventory (packages/ui)
  ds_component   — one component's Storybook story SOURCE (the API / usage / props)
  ds_stories     — the published Storybook inventory: every component and every variant
  ds_story       — one component's variants + a LIVE render URL per variant
  ds_rules       — the visual-identity rules (when to use what, what we don't do)
  ds_check       — lint code: flag hardcoded hex a token already defines
  ds_drift       — what changed since the last check (tokens + components); updates a snapshot

A recurring DRIFT WATCH (native scheduler) fires a turn on a cadence that calls ds_drift and,
if the DS moved, has the agent sync docs/consumers (a PR) or hand the lead a finding.

Also registers a **design-critic** subagent (ADR 0018): an adversarial design + a11y reviewer
that critiques a UI prototype/component against the LIVE design system (grounded via the ds_*
tools above), invoked with ``task("design-critic", <code + context>)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.design_system")

_DEFAULTS = {
    "repo": "protoLabsAI/protoContent",
    "ref": "main",
    "tokens_path": "packages/design-system/dist/tokens.json",
    # The GENERATED css — the authoritative --pl-* var names. ds_tokens prefers this over
    # the JSON because a var name is what a consumer writes; the JSON only has token paths.
    "tokens_css_path": "packages/design-system/dist/tokens.css",
    "components_path": "packages/ui/src",
    "rules_path": "docs/reference/visual-identity.md",
    "watch_cron": "0 14 * * *",
    # Root URL of the DS's published Storybook. Blank = the gallery + story tools are off
    # (the token/rules/lint tools all still work — a DS without a Storybook stays usable).
    "storybook_url": "https://protocontent-storybook.pages.dev",
}
# Register-time config snapshot (populated from registry.config in register(); rebuilt on a
# config reload). Process-local — each fleet member runs its own server process.
_CFG: dict[str, str] = dict(_DEFAULTS)


def _cfg(key: str) -> str:
    return str(_CFG.get(key) or _DEFAULTS.get(key) or "")


# ── GitHub contents API (token-authed; protoContent is private) ───────────────


_CLI_TOKEN: str | None = None  # resolved once per process; "" = probed and absent


def _gh_cli_token() -> str:
    """Token from an authed ``gh`` CLI (``gh auth token``) — the fallback when the
    process env carries none. Desktop-app workspaces inherit the app's env, which
    a Finder launch never seeds with GITHUB_TOKEN/GH_TOKEN, but the host's gh CLI
    is typically authed (it's how the github plugin works at all). Probed once and
    cached; a missing/unauthed gh degrades to unauthenticated reads exactly as
    before."""
    global _CLI_TOKEN
    if _CLI_TOKEN is None:
        import shutil
        import subprocess

        tok = ""
        gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                tok = out.stdout.strip()
        except OSError:
            tok = ""
        _CLI_TOKEN = tok
    return _CLI_TOKEN


def _headers(accept: str) -> dict[str, str]:
    h = {"Accept": accept, "User-Agent": "protoagent-design-system"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or _gh_cli_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gh_get_raw(path: str) -> str:
    """Fetch a file from ``repo@ref`` as raw text. Raises ``RuntimeError`` with a legible
    cause the tools turn into an error string."""
    import httpx

    repo, ref = _cfg("repo"), _cfg("ref")
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    try:
        r = httpx.get(url, headers=_headers("application/vnd.github.raw"), params={"ref": ref}, follow_redirects=True, timeout=20.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"could not fetch {path} from {repo}@{ref} ({type(e).__name__})") from e
    return r.text


def _gh_list(path: str) -> list[dict]:
    """List a directory in ``repo@ref`` via the contents API (a JSON array of entries)."""
    import httpx

    repo, ref = _cfg("repo"), _cfg("ref")
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    try:
        r = httpx.get(url, headers=_headers("application/vnd.github+json"), params={"ref": ref}, follow_redirects=True, timeout=20.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"could not list {path} in {repo}@{ref} ({type(e).__name__})") from e
    data = r.json()
    return data if isinstance(data, list) else []


def _component_names(entries: list[dict]) -> list[str]:
    """Component ids from a packages/ui listing — the ``<Name>`` of ``<Name>.stories.tsx``."""
    return sorted({e["name"].split(".", 1)[0] for e in entries if str(e.get("name", "")).endswith(".stories.tsx")})


# ── tools ─────────────────────────────────────────────────────────────────────


@tool
def ds_tokens() -> str:
    """The LIVE design-token vocabulary — every ``--pl-*`` custom property with its dark and
    light value. This is the vocabulary you must write styling in: never hardcode a color,
    space, radius, or duration that appears here; use ``var(--pl-…)``. Fetched live from the
    configured repo, so it reflects the system as it is now, not as it was."""
    try:
        return (
            "Live design tokens — write var(--pl-…), never the literal:\n"
            + _tokens_mod().summarize(_token_sections())
        )
    except RuntimeError as e:
        # A DS that doesn't publish a built CSS still has the source values worth reading.
        try:
            raw = _gh_get_raw(_cfg("tokens_path"))
        except RuntimeError:
            return f"ds_tokens error: {e}"
        try:
            return f"({e} — falling back to the raw token source)\nLive design tokens:\n" + json.dumps(json.loads(raw), indent=1)
        except json.JSONDecodeError:
            return raw


@tool
def ds_components() -> str:
    """List the design-system COMPONENT inventory (packages/ui) — the components the agent owns
    and should reuse/extend rather than reinvent. Live from the repo."""
    try:
        names = _component_names(_gh_list(_cfg("components_path")))
    except RuntimeError as e:
        return f"ds_components error: {e}"
    if not names:
        return f"No components found under {_cfg('components_path')}."
    return f"Components ({_cfg('components_path')}, {len(names)}):\n" + "\n".join(f"- {n}" for n in names)


@tool
def ds_component(name: str) -> str:
    """Fetch a component's Storybook story — its variants, API, and usage — by name (e.g.
    'Button', 'CommandPalette'). Read the real component before building on or changing it."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", name or "").split(".", 1)[0]
    if not safe:
        return "ds_component: provide a component name (e.g. 'Button'). Use ds_components for the inventory."
    try:
        return f"{safe}.stories.tsx:\n" + _gh_get_raw(f"{_cfg('components_path')}/{safe}.stories.tsx")
    except RuntimeError as e:
        return f"ds_component error: {e}. Check the exact name with ds_components."


@tool
def ds_rules() -> str:
    """The visual-identity RULES — when to use what, and what we don't do (the judgment layer
    over the raw token values). Read alongside ds_tokens for any design decision."""
    try:
        return _gh_get_raw(_cfg("rules_path"))
    except RuntimeError as e:
        return f"ds_rules error: {e}"


# ── token vocabulary (from the generated CSS) ─────────────────────────────────

_TOKENS_MOD = None


def _tokens_mod():
    global _TOKENS_MOD
    if _TOKENS_MOD is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("design_system_tokens", Path(__file__).resolve().parent / "tokens.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TOKENS_MOD = mod
    return _TOKENS_MOD


def _token_sections() -> list[dict]:
    """Live token vocabulary as ordered, renderable sections. Raises ``RuntimeError``."""
    tk = _tokens_mod()
    themes = tk.parse_css(_gh_get_raw(_cfg("tokens_css_path")))
    if not themes["dark"]:
        raise RuntimeError(f"no --pl-* custom properties found in {_cfg('tokens_css_path')}")
    return tk.group_tokens(themes["dark"], themes["light"])


# ── Storybook bridge ──────────────────────────────────────────────────────────
# A DS that publishes a Storybook already curates the inventory this plugin would
# otherwise reinvent: every component, every variant, each with a live render. We read
# that instead of maintaining a replica — a replica is exactly the drift this plugin exists
# to prevent. Pure parsing lives in storybook.py; the fetch + tool surface is here.

_SB_MOD = None
_SB_CACHE: tuple[float, list[dict]] | None = None  # (fetched_at, components)
_SB_TTL = 300.0  # the index moves on DS deploys, not per-turn; a 5-min cache is plenty


def _sb_mod():
    global _SB_MOD
    if _SB_MOD is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("design_system_storybook", Path(__file__).resolve().parent / "storybook.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SB_MOD = mod
    return _SB_MOD


def _sb_components(force: bool = False) -> list[dict]:
    """Parsed component inventory from the published ``index.json``, TTL-cached.

    Raises ``RuntimeError`` with a legible cause; callers turn it into an error string.
    """
    global _SB_CACHE
    import time

    base = _sb_mod().normalize_base(_cfg("storybook_url"))
    if not base:
        raise RuntimeError("no storybook_url configured — set it in Settings ▸ Plugins ▸ Design System")
    if not force and _SB_CACHE and (time.time() - _SB_CACHE[0]) < _SB_TTL:
        return _SB_CACHE[1]

    import httpx

    try:
        r = httpx.get(f"{base}/index.json", headers={"User-Agent": "protoagent-design-system"}, follow_redirects=True, timeout=20.0)
        r.raise_for_status()
        index = r.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"could not fetch the Storybook index from {base} ({type(e).__name__})") from e
    except ValueError as e:
        raise RuntimeError(f"{base}/index.json is not JSON — is storybook_url the Storybook ROOT?") from e

    comps = _sb_mod().parse_index(index)
    _SB_CACHE = (time.time(), comps)
    return comps


@tool
def ds_stories() -> str:
    """The design system's published Storybook inventory — every component and the NAME of
    every variant, grouped by the system's own taxonomy. Read this to find out what already
    exists before proposing anything new; reinventing a component the system already ships is
    the most common design-system mistake. Use ds_story for one component's live previews."""
    try:
        return "Design-system Storybook inventory (live):\n" + _sb_mod().summarize(_sb_components())
    except RuntimeError as e:
        return f"ds_stories error: {e}"


@tool
def ds_story(name: str) -> str:
    """One component's variants, each with a LIVE preview URL you can show the user — e.g.
    'Button', 'Overlays', 'Components/Layout/Grid'. Cheap and pointable: it returns the
    variant list and render URLs, NOT the source. Use ds_component for the story SOURCE
    (props / API / usage) when you need to read or change the implementation."""
    try:
        comps = _sb_components()
    except RuntimeError as e:
        return f"ds_story error: {e}"
    sb = _sb_mod()
    comp = sb.find_component(comps, name)
    if not comp:
        return f"ds_story: no component matching {name!r}. Use ds_stories for the inventory."
    base = _cfg("storybook_url")
    lines = [f"{comp['title']} — {len(comp['stories'])} variant(s)"]
    if comp.get("import_path"):
        lines.append(f"source: {comp['import_path']}  (read it with ds_component)")
    for s in comp["stories"]:
        lines.append(f"- {s['name']}: {sb.preview_url(base, s['id'])}")
    lines.append(f"open in Storybook: {sb.docs_url(base, comp['stories'][0]['id'])}" if comp["stories"] else "")
    return "\n".join(x for x in lines if x)


_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def _hex_token_map(tokens: object, path: list[str] | None = None, out: dict[str, str] | None = None) -> dict[str, str]:
    """Map every hex color value in the token tree to its dotted token path."""
    path, out = path or [], out if out is not None else {}
    if isinstance(tokens, dict):
        for k, v in tokens.items():
            _hex_token_map(v, path + [str(k)], out)
    elif isinstance(tokens, str) and _HEX_RE.fullmatch(tokens.strip()):
        out.setdefault(tokens.strip().lower(), ".".join(path))
    return out


@tool
def ds_check(code: str) -> str:
    """Lint a CSS / JSX / Tailwind snippet against the token vocabulary: flag hardcoded HEX
    colors — a value a design token already defines should be `var(--pl-…)` or the Tailwind
    utility, not a literal. The agent's 'never hardcode a color a token defines' rule, as a check."""
    if not (code or "").strip():
        return "ds_check: pass the code/CSS to check."
    try:
        tokens = json.loads(_gh_get_raw(_cfg("tokens_path")))
    except (RuntimeError, json.JSONDecodeError) as e:
        return f"ds_check error (couldn't load tokens): {e}"
    hexmap = _hex_token_map(tokens)
    findings = []
    for h in dict.fromkeys(m.lower() for m in _HEX_RE.findall(code)):  # de-duped, order-stable
        if h in hexmap:
            findings.append(f"- `{h}` → design token **{hexmap[h]}**; use var(--pl-…) or the Tailwind utility, not the literal.")
        else:
            findings.append(f"- `{h}` → not a design token; if it's a real brand value add it to @protolabsai/design, otherwise avoid the one-off.")
    if not findings:
        return "ds_check: no hardcoded hex colors — clean. ✓"
    return f"ds_check found {len(findings)} hardcoded color(s):\n" + "\n".join(findings)


# ── drift snapshot + watch ─────────────────────────────────────────────────────


def _snap_path() -> Path:
    """Instance-scoped drift snapshot (ADR 0004). ``DESIGN_SYSTEM_DIR`` overrides the base;
    ``PROTOAGENT_INSTANCE`` adds a per-member subdir so fleet members don't collide."""
    base = Path(os.environ.get("DESIGN_SYSTEM_DIR") or (Path.home() / ".protoagent" / "design-system"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base / "snapshot.json"


def _fingerprint() -> dict:
    """Current DS fingerprint — a tokens content-hash + the sorted component list."""
    tokens = _gh_get_raw(_cfg("tokens_path"))
    comps = _component_names(_gh_list(_cfg("components_path")))
    return {"tokens_sha": hashlib.sha256(tokens.encode()).hexdigest(), "components": comps}


@tool
def ds_drift() -> str:
    """What changed in the design system since the last check — token changes and components
    added/removed — then update the stored snapshot. The drift watch calls this on a cadence;
    call it any time to reconcile. First run records a baseline."""
    try:
        cur = _fingerprint()
    except RuntimeError as e:
        return f"ds_drift error: {e}"
    p = _snap_path()
    prev: dict = {}
    if p.exists():
        try:
            prev = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            prev = {}
    try:
        p.write_text(json.dumps(cur, indent=1))
    except OSError as e:
        log.warning("[design-system] could not write drift snapshot: %s", e)
    if not prev:
        return f"ds_drift: baseline recorded ({len(cur['components'])} components). No prior snapshot to diff against yet."
    changes = []
    if prev.get("tokens_sha") != cur["tokens_sha"]:
        changes.append("• TOKENS changed — run ds_tokens to see the current vocabulary and reconcile consumers.")
    added = sorted(set(cur["components"]) - set(prev.get("components", [])))
    removed = sorted(set(prev.get("components", [])) - set(cur["components"]))
    if added:
        changes.append(f"• Components ADDED: {', '.join(added)} — document them / check they use tokens.")
    if removed:
        changes.append(f"• Components REMOVED: {', '.join(removed)} — check for dangling references + docs.")
    if not changes:
        return "ds_drift: no change since the last check. ✓"
    return "Design-system DRIFT since last check:\n" + "\n".join(changes)


# ── design-critic subagent ────────────────────────────────────────────────────

_CRITIC_PROMPT = """You are the **design-critic** — an adversarial design + accessibility reviewer for
the configured design system. You are given a UI prototype or component (JSX / TSX / HTML / CSS) and what it's
for. Review it against the **live design system** and accessibility, and return concrete, prioritized
findings the author can act on. You review; you do not rewrite.

Ground every judgement in the LIVE system — don't review from memory:
- `ds_rules` — the visual-identity rules (when to use what, what we don't do). The judgment layer.
- `ds_tokens` — the current token vocabulary. Any hardcoded value a token defines is a finding.
- `ds_check` — run the code through it to catch hardcoded colors a token already defines.
- `ds_components` / `ds_component` — does a component for this already exist? Reinventing one that
  exists (Button, AppShell, Dialog, Field, …) instead of reusing it is a finding.

Review across four axes, in this priority order:
1. **Design-system adherence** — hardcoded colors/spacing/radius/type that should be `--pl-*` tokens
   or the `@pl/ui` component; reinvented components; off-system patterns. Cite the exact token/rule.
2. **Accessibility (WCAG)** — semantic elements (not div-buttons), keyboard operability + visible
   focus, color contrast, focus order, labels/alt text, ARIA only where it earns it.
3. **Layout & responsiveness** — structure, spacing rhythm, overflow, small-screen behavior.
4. **Consistency & polish** — states (hover/active/disabled/empty/loading), naming, reuse.

Output format:
- A one-line **verdict**: `ship-ready` · `revise` · `blocked`.
- Findings grouped **BLOCKER / SHOULD-FIX / NIT**, each: what's wrong, why (cite the token/rule/WCAG
  criterion), and the concrete fix (e.g. "use `var(--pl-color-brand-lavender)` / `<Button variant='primary'>`").
- A short **what's good** so the author knows what to keep.
Be specific and terse. No praise-padding. Hard stop at max_turns — return what you have."""


def _build_design_critic():
    from graph.subagents.config import SubagentConfig

    return SubagentConfig(
        name="design-critic",
        description=(
            "Adversarial design + accessibility reviewer for a UI prototype or component. Give it the "
            "code (JSX/TSX/HTML/CSS) + what it's for; it reviews against the LIVE design system (tokens, "
            "rules, existing components) and WCAG a11y and returns prioritized, actionable findings + a "
            "verdict. It reviews — it doesn't rewrite. Use it before turning a prototype into a real PR."
        ),
        system_prompt=_CRITIC_PROMPT,
        tools=["ds_rules", "ds_tokens", "ds_check", "ds_components", "ds_component"],
        max_turns=15,
    )


_WATCH_PROMPT = (
    "Design-system drift check. Call `ds_drift` to see what changed in @protolabsai/design and "
    "packages/ui since your last review. If tokens changed or components were added/removed, "
    "assess the impact on the component docs and the consuming surfaces (the marketing site, "
    "cockpit), then open a focused PR on protoContent to bring them back in sync — or, if it "
    "needs design or strategy input, write jon a tight finding. If nothing changed, do nothing."
)




_THEME_MOD = None  # loaded by path (the plugin dir isn't an importable package)


def _theme_mod():
    global _THEME_MOD
    if _THEME_MOD is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("design_system_theme", Path(__file__).resolve().parent / "theme.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _THEME_MOD = mod
    return _THEME_MOD


# ── theme-designer tools (LCH engine, ported from the operator's proto2 playground) ──
# Pure color science lives in theme.py; these tools are the agent surface. An agent can
# design a validated palette and re-theme its own console conversationally — the apply
# seam is the same {mode, overrides} blob the ThemePanel persists (theme.json, ADR 0042).


@tool
def theme_scale(base_color: str, steps: int = 11) -> str:
    """A perceptually-uniform color scale from one base color — 11 Tailwind-style stops
    (50→950) interpolated through LCH lightness (95→15), so steps LOOK evenly spaced.
    Use for building token families from a brand color. Returns JSON {stop: hex}."""
    _t = _theme_mod()
    try:
        return json.dumps(dict(zip(_t.SCALE_STOPS, _t.scale(base_color, steps))), indent=1)
    except Exception as exc:  # noqa: BLE001 — tool boundary: legible error string
        return f"Error: {exc}"


@tool
def theme_contrast(foreground: str, background: str) -> str:
    """WCAG 2.x contrast check for a color pair — ratio plus AA/AA-large/AAA verdicts.
    Check EVERY fg/bg pair you propose; never suggest a failing combination without
    flagging it. Returns JSON."""
    _t = _theme_mod()
    try:
        return json.dumps(_t.contrast(foreground, background))
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@tool
def theme_palette(base_color: str, harmony: str = "complementary", mode: str = "dark") -> str:
    """Design a FULL console palette from one base color: harmony-derived primary/
    secondary/accent scales + fixed semantic hues (success/warning/error/neutral), mapped
    onto the console's --pl-* override keys for the given mode (dark|light), with a WCAG
    contrast report for the key pairs. harmony: complementary|triadic|analogous. Review
    the contrast report, then persist with theme_apply. Returns JSON
    {palette, mode, overrides, contrast}."""
    _t = _theme_mod()
    try:
        pal = _t.palette(base_color, harmony)
        mapped = _t.overrides_for(pal, mode)
        return json.dumps({"palette": pal, **mapped}, indent=1)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@tool
def theme_apply(overrides_json: str, mode: str = "dark") -> str:
    """Apply a console theme: persist {mode, overrides} as this agent's theme (the exact
    blob the console's ThemePanel reads — takes effect on the next console load/agent
    switch). overrides_json: a JSON object of --pl-* custom properties → color values
    (theme_palette's `overrides` output, or hand-picked). Validates keys/colors and
    contrast-checks fg/bg; warnings are returned but do NOT block — the operator can
    always reset from the Theme panel. Returns JSON {ok, path, warnings}."""
    _t = _theme_mod()
    try:
        raw = json.loads(overrides_json or "{}")
        if not isinstance(raw, dict) or not raw:
            return "Error: overrides_json must be a non-empty JSON object of --pl-* keys"
        if mode not in ("dark", "light"):
            return f"Error: mode must be dark|light, got {mode!r}"
        clean, warnings = _t.validate_overrides(raw)
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"
    try:
        from graph.config_io import theme_json_path

        f = theme_json_path()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"mode": mode, "overrides": clean}, indent=2) + "\n")
        return json.dumps({"ok": True, "path": str(f), "applied": len(clean), "warnings": warnings})
    except Exception as exc:  # noqa: BLE001
        return f"Error: persisting theme failed — {exc}"


# ── console view: the design-system explorer ──────────────────────────────────
# The gallery renders the DS's OWN published Storybook (one iframe per story), so what an
# operator browses here is the real library at its current commit — not a replica this
# plugin maintains. Two routers at distinct prefixes: the PAGE is public (an iframe
# navigation can't carry a bearer), the DATA is gated.


def _build_view_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()
    page = Path(__file__).resolve().parent / "view.html"

    @router.get("/view", include_in_schema=False)
    def view() -> HTMLResponse:
        return HTMLResponse(page.read_text(encoding="utf-8"))

    return router


def _build_data_router():
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/catalog")
    def catalog() -> dict:
        """Everything the explorer renders, in one round trip.

        Tokens and stories come from different origins (the repo vs. the published
        Storybook) and fail independently, so each half carries its OWN error rather than
        failing the whole payload — a DS with no Storybook should still browse its tokens,
        and vice versa.
        """
        sb = _sb_mod()
        out: dict = {
            "meta": {
                "repo": _cfg("repo"),
                "ref": _cfg("ref"),
                "storybook_url": sb.normalize_base(_cfg("storybook_url")),
                "rules_path": _cfg("rules_path"),
            },
            "tokens": [],
            "tokens_error": None,
            "groups": [],
            "components_error": None,
        }
        try:
            out["tokens"] = _token_sections()
        except RuntimeError as e:
            out["tokens_error"] = str(e)
        try:
            comps = _sb_components()
            base = _cfg("storybook_url")
            for comp in comps:
                for s in comp["stories"]:
                    s["preview"] = sb.preview_url(base, s["id"])
                    s["docs"] = sb.docs_url(base, s["id"])
            out["groups"] = sb.group_tree(comps)
        except RuntimeError as e:
            out["components_error"] = str(e)
        return out

    @router.post("/refresh")
    def refresh() -> dict:
        """Drop the Storybook cache so a DS deploy shows up without waiting out the TTL."""
        global _SB_CACHE
        _SB_CACHE = None
        return {"ok": True}

    return router


def register(registry) -> None:
    cfg = registry.config or {}
    for k in _DEFAULTS:
        v = cfg.get(k)
        if v not in (None, ""):
            _CFG[k] = str(v)
    # View PAGE: public /plugins/design-system (ungated) — iframe nav carries no bearer.
    registry.register_router(_build_view_router(), prefix="/plugins/design-system")
    # DATA: gated /api/plugins/design-system — fetched with the handshake token.
    registry.register_router(_build_data_router(), prefix="/api/plugins/design-system")
    registry.register_tools([ds_tokens, ds_components, ds_component, ds_stories, ds_story, ds_rules, ds_check, ds_drift, theme_scale, theme_contrast, theme_palette, theme_apply])

    # design-critic subagent (ADR 0018) — reviews a prototype/component against the LIVE DS + a11y,
    # grounded via the ds_* tools above. The lead delegates to it with `task("design-critic", …)`.
    try:
        registry.register_subagent(_build_design_critic())
    except Exception:  # noqa: BLE001 — a subagent-registry hiccup must not break plugin load
        log.exception("[design-system] failed to register the design-critic subagent")

    # Arm the drift watch (native scheduler, ADR 0050) — owned by this plugin, so a disable/
    # uninstall cancels it; idempotent by job_id, so a reload re-arms cleanly.
    cron = str(cfg.get("watch_cron", _DEFAULTS["watch_cron"]) or "").strip()
    if cron:
        try:
            from graph.sdk import schedule_recurring

            res = schedule_recurring(prompt=_WATCH_PROMPT, cron=cron, plugin_id=registry.plugin_id, job_id="ds-drift-watch")
            if not res.get("ok"):
                log.warning("[design-system] drift watch not scheduled: %s", res.get("message"))
        except Exception:  # noqa: BLE001 — a scheduler hiccup must never break plugin load
            log.exception("[design-system] failed to arm the drift watch")

    log.info("[design-system] registered 12 tools + design-critic subagent (repo=%s@%s, drift-watch=%s)", _cfg("repo"), _cfg("ref"), cron or "off")
