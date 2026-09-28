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
  ds_check       — lint a snippet with the audit engine (colors, fallbacks, tokens, scales…)
  ds_audit_repo  — audit a LOCAL checkout for design-system adherence → lane-split report
  ds_site_probe_script — the in-page probe the agent runs with browser_eval on a live site
  ds_audit_url   — audit a RENDERED site (probe JSON) or, statically, a URL → same report shape
  ds_component_gaps — break a probed site into repeated UI patterns → covered / variant gap /
                   missing components, with proposed APIs and ready-to-file gap issues
  ds_drift       — what changed since the last check (tokens + components); updates a snapshot
  theme_extract / theme_probe_script / theme_generate — brand → full dark+light theme on the live contract

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
import threading
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
    # The DS's no-build kit — the .pl-* class vocabulary a prototype can render with
    # directly, no bundler. Grounds ds_design; blank disables the class list.
    "kit_css_path": "packages/ui/dist/plugin-kit.css",
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

# registry.emit, captured at register(). Plugins coordinate over the bus, never by importing
# each other (ADR 0039) — so drift is BROADCAST and anything that cares subscribes, rather
# than this plugin knowing who its consumers are.
_EMIT = None


def _emit(topic: str, data: dict) -> None:
    if _EMIT is None:
        return
    try:
        _EMIT(topic, data)
    except Exception:  # noqa: BLE001 — a bus hiccup must never break a tool call
        log.exception("[design-system] emit %s failed", topic)


def _cfg(key: str) -> str:
    """The configured value for ``key``. A key PRESENT in the live config (even set to "")
    wins over its default, so ``storybook_url: ""`` / ``watch_cron: ""`` genuinely DISABLE the
    feature instead of silently falling back — only a key ABSENT from ``_CFG`` reads through to
    ``_DEFAULTS``. (The old ``_CFG.get(key) or _DEFAULTS.get(key)`` treated a blank as unset.)"""
    val = _CFG[key] if key in _CFG else _DEFAULTS.get(key)
    return "" if val is None else str(val)


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
def ds_tokens(section: str = "") -> str:
    """The LIVE design-token vocabulary — every ``--pl-*`` custom property with its dark and
    light value. This is the vocabulary you must write styling in: never hardcode a color,
    space, radius, or duration that appears here; use ``var(--pl-…)``.

    Pass ``section`` to fetch just one family — "Color", "Space", "Typography", "Elevation",
    "Radius", "Motion", "Gradient", "Border" — when that's all you need; the full set is a few
    thousand characters and most questions only touch one family."""
    try:
        secs = _token_sections()
        want = (section or "").strip().lower()
        if want:
            secs = [x for x in secs if x["section"].lower() == want]
            if not secs:
                return f"No token section named {section!r}. Sections: " + ", ".join(
                    x["section"] for x in _token_sections()
                )
        return (
            "Live design tokens — write var(--pl-…), never the literal:\n"
            + _tokens_mod().summarize(secs)
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
def ds_search(query: str) -> str:
    """Search the design system by keyword — components, variants and tokens at once. The
    fastest way to answer "do we have a…" before building anything: it matches component and
    variant names AND token names/values, so "toast", "danger" or "spacing" all land. Use this
    first; fall back to ds_stories / ds_tokens only when you need the full inventory."""
    q = (query or "").strip().lower()
    if not q:
        return "ds_search: give a keyword (e.g. 'toast', 'danger', 'spacing')."
    hits: list[str] = []
    try:
        for comp in _sb_components():
            variants = [st["name"] for st in comp["stories"] if q in st["name"].lower()]
            if q in comp["title"].lower() or variants:
                shown = ", ".join(variants or [st["name"] for st in comp["stories"]][:4])
                hits.append(f"- COMPONENT {comp['title']} — {shown}")
    except RuntimeError as e:
        hits.append(f"- (components unavailable: {e})")
    try:
        for sec in _token_sections():
            for t in sec["tokens"]:
                if q in t["name"] or q in str(t["value"]).lower():
                    hits.append(f"- TOKEN var({t['var']}) = {t['value']}")
    except RuntimeError as e:
        hits.append(f"- (tokens unavailable: {e})")
    if not hits:
        return (
            f"No component, variant or token matches {query!r}. The system may genuinely not "
            "cover this — say so rather than inventing one, and propose an extension if it's warranted."
        )
    return f"{len(hits)} match(es) for {query!r}:\n" + "\n".join(hits[:40])


@tool
def ds_rules() -> str:
    """The visual-identity RULES — when to use what, and what we don't do (the judgment layer
    over the raw token values). Read alongside ds_tokens for any design decision."""
    try:
        return _gh_get_raw(_cfg("rules_path"))
    except RuntimeError as e:
        return f"ds_rules error: {e}"


# ── no-build class vocabulary ─────────────────────────────────────────────────

_CLASS_RE = re.compile(r"\.(pl-[a-z0-9-]+)", re.I)
_KIT_CACHE: tuple[float, list[str]] | None = None
_KIT_LOCK = threading.Lock()


def _kit_classes(force: bool = False) -> list[str]:
    """The ``.pl-*`` classes the DS's published kit CSS actually defines.

    A prototype rendered with the kit can only use classes the kit ships; a plausible-looking
    ``.pl-datepicker`` renders as an unstyled div. Reading the real stylesheet is the same
    anti-drift move as reading the real tokens.
    """
    global _KIT_CACHE
    import time

    path = _cfg("kit_css_path")
    if not path:
        raise RuntimeError("no kit_css_path configured")
    if not force and _KIT_CACHE and (time.time() - _KIT_CACHE[0]) < _FETCH_TTL:
        return _KIT_CACHE[1]
    with _KIT_LOCK:
        # Re-check under the lock: another thread may have filled the cache while we waited, so N
        # concurrent callers share ONE fetch instead of racing N (single-flight).
        if not force and _KIT_CACHE and (time.time() - _KIT_CACHE[0]) < _FETCH_TTL:
            return _KIT_CACHE[1]
        names = sorted(set(_CLASS_RE.findall(_gh_get_raw(path))))
        _KIT_CACHE = (time.time(), names)
        return names


@tool
def ds_kit_classes() -> str:
    """The design system's no-build CSS class vocabulary — every ``.pl-*`` class its published
    kit stylesheet defines. Use when writing a PROTOTYPE that renders with the kit rather than
    the React components: only these classes exist, and inventing a plausible one (.pl-datepicker)
    renders as an unstyled div. Pair with ds_tokens for values and ds_rules for judgment."""
    try:
        names = _kit_classes()
    except RuntimeError as e:
        return f"ds_kit_classes error: {e}"
    return f"Kit classes ({len(names)}) — only these exist:\n" + ", ".join(names)


# ── token vocabulary (from the generated CSS) ─────────────────────────────────

def _tokens_mod():
    return _sibling("tokens.py")


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

_SB_CACHE: tuple[float, list[dict]] | None = None  # (fetched_at, components)
_SB_LOCK = threading.Lock()
_FETCH_TTL = 300.0  # the DS moves on deploys, not per-turn; a 5-min cache is plenty


def _sb_mod():
    return _sibling("storybook.py")


def _sb_components(force: bool = False) -> list[dict]:
    """Parsed component inventory from the published ``index.json``, TTL-cached.

    Raises ``RuntimeError`` with a legible cause; callers turn it into an error string.
    """
    global _SB_CACHE
    import time

    base = _sb_mod().normalize_base(_cfg("storybook_url"))
    if not base:
        raise RuntimeError("no storybook_url configured — set it in Settings ▸ Plugins ▸ Design System")
    if not force and _SB_CACHE and (time.time() - _SB_CACHE[0]) < _FETCH_TTL:
        return _SB_CACHE[1]

    with _SB_LOCK:
        # Re-check under the lock so concurrent callers share one fetch (single-flight).
        if not force and _SB_CACHE and (time.time() - _SB_CACHE[0]) < _FETCH_TTL:
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


# ── audit engine: vocabulary + inventory (cached), ds_check, ds_audit_repo ──────
# The rules live in audit.py and the token vocabulary in vocab.py — pure modules, so a URL
# auditor can feed them fetched CSS and computed styles. This half is only the fetch + the
# tool surface.

_VOCAB_CACHE: tuple[float, tuple, object] | None = None   # (fetched_at, key, Vocab)
_INV_CACHE: tuple[float, tuple, list[str]] | None = None  # (fetched_at, key, names)
_VOCAB_LOCK = threading.Lock()
_INV_LOCK = threading.Lock()
_AUDIT_ROOTS: list[str] = []  # plugin setting `audit_roots` — extra dirs ds_audit_repo may read


def _vocab_mod():
    return _sibling("vocab.py")


def _audit_mod():
    return _sibling("audit.py")


def _vocab(force: bool = False):
    """The DS token vocabulary, TTL-cached like the kit classes. ``tokens.css`` is the
    contract; ``tokens_path`` (JSON) is only read when the CSS declares nothing. The kit
    stylesheet's own ``--pl-*`` custom properties ride along as KNOWN names (component
    vars are legitimate to reference, just not tokens). Raises ``RuntimeError``."""
    global _VOCAB_CACHE
    import time

    # The fetch seam is part of the key so a swapped `_gh_get_raw` (tests, a repointed repo)
    # never serves another source's vocabulary.
    key = (_cfg("repo"), _cfg("ref"), _cfg("tokens_css_path"), _cfg("tokens_path"), id(_gh_get_raw))
    if not force and _VOCAB_CACHE and _VOCAB_CACHE[1] == key and (time.time() - _VOCAB_CACHE[0]) < _FETCH_TTL:
        return _VOCAB_CACHE[2]
    with _VOCAB_LOCK:
        # Re-check under the lock so concurrent callers share one fetch/build (single-flight).
        if not force and _VOCAB_CACHE and _VOCAB_CACHE[1] == key and (time.time() - _VOCAB_CACHE[0]) < _FETCH_TTL:
            return _VOCAB_CACHE[2]
        vm = _vocab_mod()
        errors = []
        css = ""
        try:
            css = _gh_get_raw(_cfg("tokens_css_path"))
        except RuntimeError as e:
            errors.append(str(e))
        tokens_json = None
        if not vm.build_vocab(css).names:
            # No custom properties parsed out of the CSS (missing, empty, or a shape we can't
            # read) — the JSON token source is the fallback, never an empty vocabulary.
            try:
                tokens_json = _gh_get_raw(_cfg("tokens_path"))
            except RuntimeError as e:
                errors.append(str(e))
        extra: set[str] = set()
        if _cfg("kit_css_path"):
            try:
                extra = vm.kit_custom_properties(_gh_get_raw(_cfg("kit_css_path")))
            except RuntimeError:
                extra = set()  # the kit is optional; its absence only means fewer known component vars
        v = vm.build_vocab(css, tokens_json, extra)
        if v.source == "empty":
            raise RuntimeError("no design tokens found (" + ("; ".join(errors) or f"{_cfg('tokens_css_path')} declares no custom properties") + ")")
        _VOCAB_CACHE = (time.time(), key, v)
        return v


def _inventory(force: bool = False) -> list[str]:
    """The DS component names, for the control/shadow-component rules. Read from the
    component SOURCE modules' exports (``export function Button``) — Storybook titles are
    often groups ("Forms", "Overlays") and would miss Input/Textarea/Dialog entirely. Falls
    back to story-file names, then to Storybook titles. Never raises: an empty inventory
    makes the engine use its default control map and skip shadow-component."""
    global _INV_CACHE
    import time

    key = (_cfg("repo"), _cfg("ref"), _cfg("components_path"), id(_gh_list), id(_gh_get_raw))
    if not force and _INV_CACHE and _INV_CACHE[1] == key and (time.time() - _INV_CACHE[0]) < _FETCH_TTL:
        return _INV_CACHE[2]
    with _INV_LOCK:
        # Re-check under the lock so concurrent callers share one fetch (single-flight).
        if not force and _INV_CACHE and _INV_CACHE[1] == key and (time.time() - _INV_CACHE[0]) < _FETCH_TTL:
            return _INV_CACHE[2]
        au = _audit_mod()
        names: set[str] = set()
        try:
            entries = _gh_list(_cfg("components_path"))
            names |= set(_component_names(entries))
            sources = [
                e["name"] for e in entries
                if re.search(r"\.(tsx|jsx|ts|js)$", str(e.get("name", "")))
                and not re.search(r"\.(stories|test|spec)\.", str(e.get("name", "")))
            ][:40]
            for fn in sources:
                try:
                    names |= set(au.exported_components(_gh_get_raw(f"{_cfg('components_path')}/{fn}")))
                except RuntimeError:
                    continue
        except RuntimeError:
            pass
        if not names:
            try:
                names = {c["title"].split("/")[-1].replace(" ", "") for c in _sb_components()}
            except RuntimeError:
                names = set()
        out = sorted(names)
        _INV_CACHE = (time.time(), key, out)
        return out


def _guess_filename(code: str) -> str:
    """Pick a language for an unnamed snippet. CSS is the fallback only when the code looks
    like CSS; a bare literal or a Tailwind class string is treated as markup/JS, where any
    quoted or arbitrary-value hex is judged (not only ones inside declarations)."""
    if re.search(
        r"className=|=>|\bimport\s|<[A-Z][A-Za-z]*[\s/>]|\breturn\s*[(<]|\bexport\s+(default\s+)?(function|const)\b"
        r"|style=\{\{|\b(const|let|var)\s+\w+\s*=|\[#[0-9a-fA-F]{3,8}\]",
        code,
    ):
        return "snippet.tsx"
    if re.search(r"<(div|span|section|html|body|button|style|svg|path|circle|rect|a|p|input)\b", code, re.IGNORECASE):
        return "snippet.html"
    return "snippet.css"


def _format_findings(findings: list[dict], limit: int = 60) -> list[str]:
    lines = []
    for f in findings[:limit]:
        where = f"L{f['line']}:{f['col']} " if f.get("line") else ""
        tail = f" → {f['suggestion']}" if f.get("suggestion") else ""
        lines.append(f"- {where}[{f['rule']}·{f['severity']}] {f['message']}{tail}")
    if len(findings) > limit:
        lines.append(f"- … +{len(findings) - limit} more")
    return lines


@tool
def ds_check(code: str, filename: str = "") -> str:
    """Lint a CSS / SCSS / JSX / TSX / HTML / Tailwind snippet against the LIVE design system —
    the same rule engine ds_audit_repo runs over a whole repo. Flags: literal colors anywhere,
    including Tailwind arbitrary values like `bg-[#9b87f2]` (with the exact or nearest
    `var(--pl-…)` token / Tailwind utility to use and its ΔE), `var(--pl-x, fallback)` whose
    fallback no longer matches the token, `var(--pl-…)` names the DS doesn't define, hardcoded font-size/radius/
    spacing/shadow values where a token scale exists, app CSS restyling `.pl-*` classes, local
    aliases of token values, raw <button>/<input>/… where the DS ships a component, and imports
    of competing UI kits. `filename` (e.g. "Card.tsx", "card.css") picks the language; omitted,
    it is guessed from the code. Run it over anything you write before showing it.
    Suppress a deliberate line with a `ds-audit-ignore` comment."""
    if not (code or "").strip():
        return "ds_check: pass the code/CSS to check."
    try:
        vocab = _vocab()
    except RuntimeError as e:
        return f"ds_check error (couldn't load tokens): {e}"
    au = _audit_mod()
    fname = (filename or "").strip() or _guess_filename(code)
    inventory: list[str] = []
    if au.file_kind(fname) in ("js", "sfc"):
        try:
            inventory = _inventory()
        except Exception:  # noqa: BLE001 — the inventory only sharpens two rules
            inventory = []
    ctx = au.AuditContext(inventory=inventory)
    # Unnamed snippets are fragments of unknown shape: `loose` judges every hex when there
    # are no CSS declarations to anchor on ("#ff0000", "bg-[#9b87f2] text-white").
    findings = au.audit_text(code, fname, vocab, ctx=ctx, loose=not (filename or "").strip())
    findings, _summary = au.finalize(findings, ctx, vocab)
    if not findings:
        return "ds_check: no hardcoded colors, stale fallbacks, unknown tokens or off-system patterns — clean. ✓"
    consumer = sorted((f for f in findings if f["lane"] == "consumer"), key=lambda f: (f["line"], f["col"]))
    gaps = [f for f in findings if f["lane"] == "ds"]
    out = [f"ds_check found {len(consumer)} issue(s) in {fname}:"] + _format_findings(consumer)
    if gaps:
        out += ["", "Design-system gaps (not this code's fault — the DS lacks something it needed):"] + _format_findings(gaps, 10)
    return "\n".join(out)


def _host_config():
    """The live host config (``graph.sdk.config()``), or None outside a host. A seam: tests
    monkeypatch it rather than standing up a runtime."""
    try:
        from graph.sdk import config

        return config()
    except Exception:  # noqa: BLE001 — no host (tests, a bare import): no host-granted roots
        return None


def _registered_projects() -> list[dict]:
    cfg = _host_config()
    out: list[dict] = []
    if cfg is None:
        return out
    for key in ("projects", "filesystem_projects"):
        for p in getattr(cfg, key, None) or []:
            if isinstance(p, dict) and p.get("path"):
                out.append({**p, "_source": "managed project" if key == "projects" else "work folder"})
    return out


def _allowed_roots() -> list[tuple[str, Path]]:
    """Where ds_audit_repo may read: the host's project-onboarding root (where
    ``onboard_project`` clones), every registered project / work folder (ADR 0095 / 0007 —
    the operator already granted the agent those), and this plugin's ``audit_roots``."""
    roots: list[tuple[str, Path]] = []
    cfg = _host_config()
    onboarding = str(getattr(cfg, "onboarding_root", "") or "").strip() if cfg is not None else ""
    if onboarding:
        roots.append(("onboarding root", Path(onboarding).expanduser()))
    for p in _registered_projects():
        roots.append((f"{p['_source']} {p.get('name') or p['path']}", Path(str(p["path"])).expanduser()))
    for r in _AUDIT_ROOTS:
        if str(r).strip():
            roots.append(("audit_roots", Path(str(r).strip()).expanduser()))
    out = []
    for label, path in roots:
        try:
            out.append((label, path.resolve()))
        except OSError:
            continue
    return out


def _legacy_data_dir() -> Path:
    """The pre-SDK location (``~/.protoagent/design-system[/<instance>]``) — wrong for a fleet
    member whose instance root isn't under ~/.protoagent, kept only as a fallback + migration source."""
    base = Path.home() / ".protoagent" / "design-system"
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    return base / inst if inst else base


def _data_dir() -> Path:
    """This plugin's instance-scoped data dir (ADR 0004/0065).

    ``DESIGN_SYSTEM_DIR`` overrides (tests, operators); otherwise the host's
    ``graph.sdk.plugin_store`` — the instance root the host resolves via ``instance_paths()``
    (``PROTOAGENT_HOME``), so the dev sandbox and every fleet member get their own copy. A host
    older than that seam (< 0.148) falls back to the legacy home-dir path."""
    override = os.environ.get("DESIGN_SYSTEM_DIR")
    if override:
        base = Path(override)
        inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
        if inst:
            base = base / inst
    else:
        try:
            from graph.sdk import plugin_store

            base = plugin_store(plugin_id="design-system")
        except Exception:  # noqa: BLE001 — no host / older host: the legacy path still works
            base = _legacy_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base


REPORTS_KEPT = 10  # per audited target; older report pairs are deleted on each new audit


def _prune_reports(out_dir: Path, name: str, keep: int = REPORTS_KEPT) -> None:
    """Keep the newest ``keep`` report pairs for one target — reports are big and a scheduled
    audit would otherwise grow the data dir forever. Stamps sort chronologically."""
    for ext in (".md", ".json"):
        pat = re.compile(re.escape(name) + r"-\d{8}-\d{6}" + re.escape(ext) + "$")
        mine = sorted(p for p in out_dir.iterdir() if pat.match(p.name))
        for old in mine[:-keep] if keep > 0 else mine:
            try:
                old.unlink()
            except OSError:
                pass


_SLUG_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


@tool
def ds_audit_repo(path: str, include: str = "", exclude: str = "", rules: str = "", max_findings: int = 400) -> str:
    """Audit a LOCAL checkout for design-system adherence and write a full report.

    `path`: a directory on this host — or a GitHub `owner/repo` that is already checked out
    as a registered project. It must live under the project-onboarding root, a registered
    project / work folder, or this plugin's `audit_roots` setting; anything else is refused
    with the remedy. This tool never clones: for a repo that isn't on disk yet, run
    `onboard_project("owner/repo")` first, then call this again.

    `include` / `exclude`: comma-separated globs on the path relative to `path` (e.g.
    include="apps/web/src/*"). Always skipped: dot-directories, node_modules, dist, build, out,
    vendor(ed), third_party, coverage, framework caches (.next, .svelte-kit, …), test dirs
    (tests, test, __tests__, e2e, fixtures, __mocks__, __snapshots__), storybook-static,
    *.test.*, *.spec.*, *.stories.*, *.d.ts, *.min.*, the token file itself, symlinks and
    special files. `public/` IS scanned. The audit stops after 60 s and says so. `rules`: comma-separated rule
    ids to run (default all): raw-color, stale-fallback, unknown-token, off-scale-length,
    ds-class-override, legacy-alias, hand-rolled-control, shadow-component, foreign-ui-lib,
    missing-scale, palette-gap, override-hotspot. `max_findings` caps what THIS reply shows
    (most severe first); the report files always hold everything.

    Returns a markdown summary — a 0-100 adherence score, counts by rule, and findings grouped
    by theme with file:line evidence, split into two LANES: design-system gaps (file in the DS
    repo: "add a type scale") and consumer violations (fix in the audited repo: "use
    var(--pl-x)") — plus the paths of the full .md and .json reports. Verify the top findings
    by reading the cited lines before filing anything."""
    raw = (path or "").strip()
    if not raw:
        return "ds_audit_repo: pass a local path (or an onboarded `owner/repo`)."
    target: Path | None = None
    slug = _SLUG_RE.match(raw)
    if slug and not raw.startswith((".", "/", "~")) and not Path(raw).expanduser().exists():
        want = slug.group(1).lower()
        for p in _registered_projects():
            gh = str(p.get("github") or "").lower().removeprefix("https://github.com/").strip("/")
            if gh == want:
                target = Path(str(p["path"])).expanduser()
                break
        if target is None:
            return (
                f"ds_audit_repo: {slug.group(1)} isn't checked out on this host. Run "
                f"onboard_project(\"{slug.group(1)}\") first — it clones under the onboarding root and "
                "registers the project — then call ds_audit_repo again with that repo or the path it reports. "
                "(This tool reads local checkouts only; it never clones.)"
            )
    if target is None:
        target = Path(raw).expanduser()
    try:
        target = target.resolve()
    except OSError as e:
        return f"ds_audit_repo: can't resolve {raw}: {e}"
    if not target.is_dir():
        return f"ds_audit_repo: {target} is not a directory on this host."
    roots = _allowed_roots()
    if not any(target == r or target.is_relative_to(r) for _, r in roots):
        listed = "; ".join(f"{label}: {r}" for label, r in roots) or "none configured"
        return (
            f"Refused: {target} is outside every root this tool may read ({listed}). Remedy: "
            "onboard it with onboard_project (clones under the onboarding root), register it as a "
            "project in Settings ▸ Projects, or add its parent directory to this plugin's "
            "`audit_roots` setting (Settings ▸ Plugins ▸ Design System)."
        )
    au = _audit_mod()
    wanted = [r.strip() for r in (rules or "").split(",") if r.strip()]
    unknown = [r for r in wanted if r not in au.RULES]
    if unknown:
        return f"ds_audit_repo: unknown rule id(s) {', '.join(unknown)}. Valid: {', '.join(au.RULES)}"
    try:
        vocab = _vocab()
    except RuntimeError as e:
        return f"ds_audit_repo error (couldn't load the design system's tokens): {e}"
    try:
        inventory = _inventory()
    except Exception:  # noqa: BLE001 — the inventory only sharpens two rules; never fail the audit on it
        inventory = []
    result = au.audit_tree(target, vocab, inventory=inventory, include_globs=include, exclude_globs=exclude, rules=wanted or None)
    findings, summary = result["findings"], result["summary"]

    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", target.name) or "repo"
    out_dir = _data_dir() / "audits"
    title = f"Design-system audit — {target}"
    report_md = report_json = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        report_md = out_dir / f"{name}-{stamp}.md"
        report_json = out_dir / f"{name}-{stamp}.json"
        report_md.write_text(au.render_markdown(findings, summary, title=title, per_group=25, groups_per_rule=100, vocab=vocab), encoding="utf-8")
        report_json.write_text(au.render_json(findings, summary, root=str(target), vocab=vocab), encoding="utf-8")
        _prune_reports(out_dir, name)
    except OSError as e:
        log.warning("[design-system] could not write audit report: %s", e)

    order = {"error": 0, "warn": 1, "info": 2}
    ds_lane = [f for f in findings if f["lane"] == "ds"]
    consumer = sorted((f for f in findings if f["lane"] == "consumer"), key=lambda f: order.get(f["severity"], 3))
    try:
        cap = max(1, int(max_findings or 400))
    except (TypeError, ValueError):
        cap = 400
    shown = ds_lane + consumer[:cap]
    md = au.render_markdown(shown, summary, title=title, per_group=4, groups_per_rule=8, vocab=vocab)
    notes = []
    if len(consumer) > cap:
        notes.append(f"_This reply shows the {cap} most severe of {len(consumer)} consumer findings; the report files hold all of them._")
    if summary.get("truncated"):
        notes.append(f"_File limit reached ({summary.get('max_files')} files) — narrow with `include`._")
    if not inventory:
        notes.append("_Component inventory unavailable — hand-rolled-control used the default map and shadow-component was skipped._")
    if report_md:
        notes.append(f"Full report: `{report_md}` (markdown) · `{report_json}` (JSON, every finding).")
    return md + ("\n" + "\n".join(notes) if notes else "")


# ── site audit: rendered probe (browser_eval) or static fetch ────────────────────
# Pure analysis lives in siteprobe.py (+ the probe JS in siteprobe_js.py); the guarded fetch
# in fetch.py. Browser rendering is NOT done here: plugins never import each other, so the
# agent composes core's browser tools (browser_open + browser_eval) with these.

_URL_MAX_HTML = 2_000_000
_URL_MAX_CSS = 1_500_000
_URL_MAX_TOTAL = 5_000_000
_URL_MAX_SHEETS = 10
_URL_BUDGET_S = 40.0


def _siteprobe_mod():
    return _sibling("siteprobe.py")


def _fetch_mod():
    return _sibling("fetch.py")


def _url_fetch(url: str, max_bytes: int, deadline: float, allow_offsite_hosts=False) -> tuple[str, str]:
    """The network seam for static URL mode (tests monkeypatch this). Public hosts only, the
    connection pinned to the checked IP; ``deadline`` is an ABSOLUTE ``time.monotonic()`` value
    shared by every fetch of one read — see fetch.py. Raises RuntimeError (FetchError)."""
    return _fetch_mod().fetch_text(url, max_bytes=max_bytes, deadline=deadline, allow_offsite_hosts=allow_offsite_hosts)


def _fetch_site(url: str) -> tuple[str, str, list[tuple[str, str]], list[str]]:
    """Static read: (final_url, html, [(sheet_url, css)], notes). The HTML plus the stylesheets
    it links (and their @imports) — same-site / common-CDN sheets first, then any other PUBLIC
    host the page itself links (a site's CSS often lives on its own CDN domain); each fetch goes
    through the guarded fetcher, and the whole read is capped in sheets, bytes and time."""
    import time

    sp = _siteprobe_mod()
    deadline = time.monotonic() + _URL_BUDGET_S
    # The page itself may only redirect within its own site; the stylesheets it links may live
    # on any PUBLIC host (a site's CSS often sits on its own CDN domain).
    final, html = _url_fetch(url, _URL_MAX_HTML, min(deadline, time.monotonic() + 15.0), allow_offsite_hosts=False)
    assets = sp.site_assets(html, final)
    total = len(html)
    sheets: list[tuple[str, str]] = []
    skipped, failed = [], []
    linked = list(assets["stylesheets"])
    queue = [u for u in linked if sp.stylesheet_allowed(u, final)] + [u for u in linked if not sp.stylesheet_allowed(u, final)]
    seen: set[str] = set()
    while queue:
        sheet = queue.pop(0)
        if sheet in seen:
            continue
        seen.add(sheet)
        if len(sheets) >= _URL_MAX_SHEETS or deadline - time.monotonic() <= 1 or total > _URL_MAX_TOTAL:
            skipped.append(sheet)
            continue
        try:
            got_url, css = _url_fetch(sheet, _URL_MAX_CSS, min(deadline, time.monotonic() + 15.0), allow_offsite_hosts=True)
        except Exception as e:  # noqa: BLE001 — one hostile stylesheet must not kill the read
            failed.append(f"{sheet} ({e})")
            continue
        total += len(css)
        sheets.append((got_url, css))
        queue.extend(u for u in sp.css_imports(css, got_url) if u not in seen)
    notes = [f"static read of {final}: HTML + {len(sheets)} stylesheet(s); {len(skipped)} skipped (over the sheet/byte/time budget), {len(failed)} failed"]
    notes += [f"failed: {f}" for f in failed[:5]]
    return final, html, sheets, notes


def _report_paths(kind: str, label: str) -> tuple[Path, Path]:
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:60] or kind
    out_dir = _data_dir() / kind
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{name}-{stamp}.md", out_dir / f"{name}-{stamp}.json"


def _url_label(url: str) -> str:
    from urllib.parse import urlparse

    u = urlparse(url or "")
    return (u.hostname or "site") + (u.path.rstrip("/").replace("/", "_") if u.path and u.path != "/" else "")


# Fewer visible elements than this is not a page to judge — almost always a probe taken
# before a client-rendered page (a Storybook story, an SPA route) mounted. #27: an empty
# probe used to score a meaningless 100/100.
_MIN_VISIBLE = 5


def _load_probe(text: str):
    """(merged probe, None) or (None, error string). A probe that saw (almost) nothing
    is refused rather than judged: no score, no theme."""
    sp = _siteprobe_mod()
    try:
        probe = sp.merge_probes(sp.parse_probe(text))
    except (ValueError, RecursionError) as e:
        return None, str(e)[:500]
    visible = int(probe.get("visible") or 0)
    if visible < _MIN_VISIBLE:
        return None, (
            f"the probe saw only {visible} visible element(s), so there is nothing to judge — no verdict. "
            "The page most likely hadn't rendered yet (a Storybook story or an SPA mounts after load). "
            "Wait for it (browser_wait for a selector, or a few seconds), re-run ds_site_probe_script with "
            "browser_eval, and pass the new probe."
        )
    return probe, None


@tool
def ds_site_probe_script(script_only: bool = False) -> str:
    """The in-page PROBE for auditing / decomposing a live site — the first step of
    ds_audit_url (rendered mode) and ds_component_gaps. Returns one JavaScript expression plus
    how to run it; with `script_only=True`, just the expression (for an execute_code script).

    Run it with the browser tools: `browser_open(url)` → (let the page settle; for a SPA take a
    `browser_snapshot` first) → `browser_eval(<the expression>)`. It returns a JSON string
    (≤ 30 KB, hard-capped; the script itself is ~24 KB) describing the RENDERED page: computed colors / type / radii /
    spacing / shadows with counts and example selectors, text-vs-background pairs, :root
    custom properties, DS class usage, landmarks, forms, and the page's REPEATED UI PATTERNS
    (clustered, each with a count, inferred kind, trimmed markup, sizes, states, a11y name).
    Pass that JSON — exactly as browser_eval returned it — to `ds_audit_url` and
    `ds_component_gaps`. Several pages: pass a JSON array of the results to either tool."""
    sp = _siteprobe_mod()
    try:
        prefix = (_vocab().prefix or "--pl-").strip("-")
    except RuntimeError:
        prefix = "pl"
    js = sp.probe_script(prefix)
    if script_only:
        return js
    return (
        "Site probe — run on a page opened with browser_open:\n"
        "1. browser_open(url); wait for it to settle (a SPA: browser_snapshot once, or re-run if `visible` comes back tiny).\n"
        "2. browser_eval(expression=<the script below>) → a JSON string.\n"
        "3. ds_audit_url(url_or_probe=<that string>) — adherence to the DS; ds_component_gaps(probe=<that string>) — what components the site needs.\n"
        "4. Optional: browser_screenshot() and LOOK at it — the pattern kinds are heuristics; confirm the top ones by eye.\n"
        "Several pages: collect each result and pass a JSON array of them ([\"<probe1>\", \"<probe2>\"]) to merge.\n"
        "With execute_code (if enabled and allowed to call these tools) do it in one script so the JSON never passes through you:\n"
        "  js = tools.ds_site_probe_script(script_only=True)\n"
        "  probes = []\n"
        "  for u in urls: tools.browser_open(url=u); probes.append(tools.browser_eval(expression=js))\n"
        "  import json; p = json.dumps(probes); print(tools.ds_audit_url(url_or_probe=p)[:2500]); print(tools.ds_component_gaps(probe=p)[:3000])\n\n"
        + js
    )


_MAX_REPLY = 24_000


def _cap_reply(text: str) -> str:
    """Keep a tool reply inside a sane context budget; the report files hold everything."""
    if len(text) <= _MAX_REPLY:
        return text
    cut = text.rfind("Full report:")
    tail = text[cut:] if cut > 0 and len(text) - cut < 2000 else ""
    return text[: _MAX_REPLY - len(tail)] + "\n\n_…reply truncated — the report files hold everything._\n" + tail


def _rules_arg(rules: str, valid: dict) -> tuple[list[str], str | None]:
    wanted = [r.strip() for r in (rules or "").split(",") if r.strip()]
    unknown = [r for r in wanted if r not in valid]
    if unknown:
        return [], f"unknown rule id(s) {', '.join(unknown)}. Valid: {', '.join(valid)}"
    return wanted, None


@tool
def ds_audit_url(url_or_probe: str, rules: str = "") -> str:
    """Audit a live SITE for design-system adherence → the same 0-100 score, lanes and report
    shape as ds_audit_repo.

    `url_or_probe` is EITHER:
    - the probe JSON from `browser_eval(ds_site_probe_script)` (or a JSON array of several
      pages' probes) — the RENDERED truth, and the preferred input: every computed color is
      matched to the nearest DS token (ΔE2000), font sizes / radii / spacing / shadows are
      checked against the token scales, the page's --pl-* adoption and token values are
      checked, text/background pairs are WCAG contrast-checked, and interactive patterns with
      no accessible name are flagged;
    - a URL — a STATIC fallback when the browser tools are off: the HTML and its same-site /
      CDN stylesheets are fetched (public hosts only) and run through the repo auditor's CSS
      rules. No JavaScript runs, so CSS-in-JS and runtime theming are invisible; the report
      says so. Prefer the probe.

    `rules`: comma-separated rule ids to run (default all). Writes the full .md + .json report
    to the plugin data dir and returns the summary plus their paths. Findings are heuristics
    over a rendered page — check the top ones (screenshot / the cited selector) before filing."""
    try:
        return _cap_reply(_ds_audit_url(url_or_probe, rules))
    except Exception as e:  # one hostile page/stylesheet must not kill the tool call
        log.exception("[design-system] ds_audit_url failed")
        return f"ds_audit_url error: {type(e).__name__}: {str(e)[:300]}"


def _ds_audit_url(url_or_probe: str, rules: str = "") -> str:
    raw = (url_or_probe or "").strip()
    if not raw:
        return "ds_audit_url: pass the probe JSON (preferred — see ds_site_probe_script) or a URL."
    sp = _siteprobe_mod()
    au = _audit_mod()
    try:
        vocab = _vocab()
    except RuntimeError as e:
        return f"ds_audit_url error (couldn't load the design system's tokens): {e}"
    if sp.looks_like_probe(raw):
        wanted, err = _rules_arg(rules, sp.URL_RULES)
        if err:
            return f"ds_audit_url: {err}"
        probe, err = _load_probe(raw)
        if err:
            return f"ds_audit_url: {err}"
        findings, stats = sp.audit_probe(probe, vocab, rules=wanted or None)
        summary = sp.summarize_url(findings, stats)
        stats_line = sp.stats_line_rendered(summary, probe)
        pages = summary["pages"]
        title = f"Design-system audit (rendered) — {pages[0]}" + (f" +{len(pages) - 1} pages" if len(pages) > 1 else "")
        label = _url_label(pages[0] if pages else "")
        if probe.get("truncated"):
            summary["probe_truncated"] = probe["truncated"]
    else:
        wanted, err = _rules_arg(rules, sp.REPORT_RULES)
        if err:
            return f"ds_audit_url: {err}"
        rendered_only = [r for r in wanted if r in sp.RENDERED_ONLY]
        if rendered_only:
            return (f"ds_audit_url: {', '.join(rendered_only)} only apply to a RENDERED probe — a static URL read has no computed "
                    "styles, contrast pairs or accessible names. Run ds_site_probe_script with browser_eval and pass the probe, or drop those rules.")
        url = raw if re.match(r"^https?://", raw, re.IGNORECASE) else "https://" + raw
        try:
            final, html, sheets, notes = _fetch_site(url)
        except RuntimeError as e:
            return (f"ds_audit_url: could not read {url} statically ({e}). If the browser tools are on, run the probe "
                    "instead (ds_site_probe_script → browser_eval) — it also works for pages a static fetch can't reach.")
        try:
            inventory = _inventory()
        except Exception:  # noqa: BLE001 — the inventory only sharpens two rules
            inventory = []
        findings, summary = sp.static_audit(html, final, sheets, vocab, inventory=inventory, rules=wanted or None, notes=notes)
        stats_line = sp.stats_line_static(summary)
        title = f"Design-system audit (STATIC, no JS) — {final}"
        label = _url_label(final) + "-static"
    report_md = report_json = None
    try:
        report_md, report_json = _report_paths("audits", label)
        report_md.write_text(au.render_markdown(findings, summary, title=title, per_group=25, groups_per_rule=100, vocab=vocab,
                                                registry=sp.REPORT_RULES, stats_line=stats_line), encoding="utf-8")
        report_json.write_text(au.render_json(findings, summary, root=title, vocab=vocab, registry=sp.REPORT_RULES), encoding="utf-8")
    except OSError as e:
        log.warning("[design-system] could not write URL audit report: %s", e)
    md = au.render_markdown(findings, summary, title=title, per_group=3, groups_per_rule=8, vocab=vocab, registry=sp.REPORT_RULES, stats_line=stats_line)
    notes = list(summary.get("notes") or [])
    if summary.get("probe_truncated"):
        notes.append(f"_The probe trimmed itself to fit its size cap ({', '.join(summary['probe_truncated'][:3])}…): the rarest values/patterns are not in it._")
    if report_md:
        notes.append(f"Full report: `{report_md}` (markdown) · `{report_json}` (JSON, every finding).")
    return md + ("\n" + "\n".join(notes) if notes else "")


@tool
def ds_component_gaps(probe: str) -> str:
    """Break a probed site down into its REPEATED UI PATTERNS and match each against the
    design system's component inventory — where the DS already covers the site, where a DS
    component lacks a variant the site needs, and where a NEW component is warranted.

    `probe`: the JSON `browser_eval` returned for `ds_site_probe_script` (or a JSON array of
    several pages' probes — patterns are merged across pages by structure).

    Each pattern group is classified:
    - COVERED — the DS ships it (e.g. Button, Tabs, Dialog); the site should compose it;
    - VARIANT GAP — the DS component exists but its published stories don't show a size /
      shape / variant / state the site uses (says which);
    - MISSING — no DS component for this kind (breadcrumb, pagination, carousel…): a candidate
      new component with its count, representative markup, a proposed name + props API
      inferred from the variation seen, and a priority (frequency × prominence);
    - UNCLASSIFIED — a repeated boxed pattern the heuristics can't name: LOOK at it.
    Returns markdown with a ready-to-file issue per MISSING / VARIANT GAP in the DS gap format
    (## Gap / ## Evidence / ## Proposed API / ## Priority / ## Context), and writes .md + .json
    to the plugin data dir. Kinds are heuristics — screenshot and verify before filing."""
    try:
        return _cap_reply(_ds_component_gaps(probe))
    except Exception as e:  # one hostile page/stylesheet must not kill the tool call
        log.exception("[design-system] ds_component_gaps failed")
        return f"ds_component_gaps error: {type(e).__name__}: {str(e)[:300]}"


def _ds_component_gaps(probe: str) -> str:
    raw = (probe or "").strip()
    if not raw:
        return "ds_component_gaps: pass the probe JSON (run ds_site_probe_script's script with browser_eval first)."
    sp = _siteprobe_mod()
    merged, err = _load_probe(raw)
    if err:
        return f"ds_component_gaps: {err}"
    inventory = _inventory()
    try:
        sb = _sb_components()
    except RuntimeError:
        sb = None
    try:
        vocab = _vocab()
    except RuntimeError:
        vocab = None
    result = sp.component_gaps(merged, inventory, sb, vocab)
    notes = []
    if not inventory:
        notes.append("_Component inventory unavailable (couldn't read the DS repo) — every pattern reads as MISSING; fix access before trusting this._")
    if sb is None:
        notes.append("_Storybook unavailable — variant gaps can't be checked; covered components are marked unverified._")
    report_md = None
    try:
        report_md, report_json = _report_paths("gaps", _url_label(result["url"]))
        report_md.write_text(sp.render_gaps_markdown(result, ds_repo=_cfg("repo"), report_path=str(report_md)), encoding="utf-8")
        report_json.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        notes.append(f"Full report: `{report_md}` · `{report_json}`.")
    except OSError as e:
        log.warning("[design-system] could not write gap report: %s", e)
    return sp.render_gaps_markdown(result, ds_repo=_cfg("repo"), report_path=str(report_md or "")) + ("\n" + "\n".join(notes) if notes else "")


# ── drift snapshot + watch ─────────────────────────────────────────────────────


def _migrate_legacy_dirs(legacy_dir: Path, cur: Path) -> None:
    """Copy the legacy ``audits/`` and ``themes/`` dirs into the SDK store once — only a subdir
    the new path doesn't already have, so a re-run never clobbers fresh output. Best-effort: a
    copy failure is logged and skipped, never fatal to the caller."""
    import shutil

    for sub in ("audits", "themes"):
        src, dst = legacy_dir / sub, cur / sub
        if src.is_dir() and not dst.exists():
            try:
                shutil.copytree(src, dst)
            except OSError as e:
                log.warning("[design-system] could not migrate legacy %s dir: %s", sub, e)


def _snap_path() -> Path:
    """Instance-scoped drift snapshot. Migrates a snapshot left at the legacy path — and, once,
    the legacy audits/ and themes/ report dirs — so moving to the SDK store doesn't cost the next
    drift check its baseline or strand past reports/themes at the old home-dir path."""
    cur = _data_dir()
    p = cur / "snapshot.json"
    legacy_dir = _legacy_data_dir()
    if legacy_dir != cur and not os.environ.get("DESIGN_SYSTEM_DIR"):
        legacy = legacy_dir / "snapshot.json"
        if not p.exists() and legacy.exists():
            try:
                p.write_text(legacy.read_text())
            except OSError:
                pass
        _migrate_legacy_dirs(legacy_dir, cur)
    return p


def _fingerprint() -> dict:
    """Current DS fingerprint — a tokens content-hash + the sorted component list."""
    tokens = _gh_get_raw(_cfg("tokens_path"))
    comps = _component_names(_gh_list(_cfg("components_path")))
    return {"tokens_sha": hashlib.sha256(tokens.encode()).hexdigest(), "components": comps}


# Serialises the whole read→diff→emit→persist of a drift check, so a scheduled call and a manual
# one can't both diff against — and emit design-system.drift-detected for — the same prior
# snapshot before either writes the new one.
_DRIFT_LOCK = threading.Lock()


def _persist_snapshot(p: Path, cur: dict) -> None:
    """Write the current fingerprint as the new baseline. Isolated so the drift ordering (persist
    LAST, after the diff is built and emitted) is testable and a write failure stays non-fatal."""
    p.write_text(json.dumps(cur, indent=1))


@tool
def ds_drift() -> str:
    """What changed in the design system since the last check — token changes and components
    added/removed — then update the stored snapshot. The drift watch calls this on a cadence;
    call it any time to reconcile. First run records a baseline."""
    with _DRIFT_LOCK:
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

        def _persist() -> None:
            # Persisted LAST and non-fatally: a snapshot write failure must not swallow a drift
            # the caller (and any subscriber) still needs to see.
            try:
                _persist_snapshot(p, cur)
            except OSError as e:
                log.warning("[design-system] could not write drift snapshot: %s", e)

        if not prev:
            _persist()
            return f"ds_drift: baseline recorded ({len(cur['components'])} components). No prior snapshot to diff against yet."

        tokens_changed = prev.get("tokens_sha") != cur["tokens_sha"]
        added = sorted(set(cur["components"]) - set(prev.get("components", [])))
        removed = sorted(set(prev.get("components", [])) - set(cur["components"]))
        changes = []
        if tokens_changed:
            changes.append("• TOKENS changed — run ds_tokens to see the current vocabulary and reconcile consumers.")
        if added:
            changes.append(f"• Components ADDED: {', '.join(added)} — document them / check they use tokens.")
        if removed:
            changes.append(f"• Components REMOVED: {', '.join(removed)} — check for dangling references + docs.")

        if not changes:
            _persist()
            return "ds_drift: no change since the last check. ✓"

        # Build the result and broadcast it (consumers subscribe by topic rather than this plugin
        # knowing who they are), THEN persist — the diff is computed against `prev`, so a failed
        # write can't corrupt what we return or emit.
        _emit("drift-detected", {
            "repo": _cfg("repo"),
            "ref": _cfg("ref"),
            "tokens_changed": tokens_changed,
            "components_added": added,
            "components_removed": removed,
        })
        _persist()
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


_EXPLAINER_PROMPT = """You are the **design-system explainer** — you answer questions about THIS design
system, grounded in what it actually contains right now.

You are not a general design consultant. Every claim you make must come from a tool call in this
turn, because the system changes and your training data is not it:
- `ds_rules` — the visual-identity rules: when to use what, and what we don't do. The judgment layer.
- `ds_tokens` — the live `--pl-*` vocabulary, with dark and light values.
- `ds_stories` — every component and every variant the published Storybook ships.
- `ds_story <name>` — one component's variants, each with a live preview URL.
- `ds_component <name>` — a component's story SOURCE, for props and real usage.
- `ds_check <code>` — whether a snippet hardcodes a value a token already defines.

How to answer:
- **Lead with the answer.** A sentence or two, then the evidence. No preamble.
- **Name the real thing** — the exact token (`var(--pl-color-status-error)`), the exact component
  and variant (`Button`, `variant="danger"`). A name you didn't read from a tool is a guess; don't
  offer it.
- **Prefer what exists.** If the system already ships something for this, say so and point at it.
  Reinventing a component the system has is the most common and most expensive mistake here.
- **Say when it doesn't cover this.** "The system has no X yet" is a genuinely useful answer, and far
  better than inventing a plausible-sounding token or variant. If a reasonable extension exists,
  describe it as a proposal and label it as one.
- **Link a preview when it helps.** `ds_story` gives you a live URL per variant; a reader can click it.
- Be concise. Markdown, short sections, no padding. If the question is ambiguous, answer the most
  likely reading and note the other briefly rather than asking and stalling."""


# The explainer's toolset, as objects — the allowlist is derived from it so a rename can't
# silently drop a tool. These resolve from the lead agent's bound tool map at dispatch, which
# is the normal path for a chat- or task()-driven subagent.
def _explainer_tools() -> list:
    return [ds_rules, ds_tokens, ds_components, ds_component, ds_stories, ds_story, ds_check]


def _build_explainer():
    from graph.subagents.config import SubagentConfig

    return SubagentConfig(
        name="ds-explainer",
        description=(
            "Answers a question about the design system — which component or token to use, what a "
            "variant is for, whether the system covers a case — grounded in the LIVE tokens, rules "
            "and component inventory rather than from memory. Use for 'what should I use for…' / "
            "'do we have a…' / 'what's our … scale' questions."
        ),
        system_prompt=_EXPLAINER_PROMPT,
        tools=[t.name for t in _explainer_tools()],
        max_turns=12,
    )


_DESIGNER_PROMPT = """You are the **design-system designer** — you turn a design intent into a working
prototype built out of THIS system, so a person can look at it and react.

Ground everything in the live system before you write a line of markup:
- `ds_rules` — the visual-identity rules. What we do, and specifically what we don't.
- `ds_kit_classes` — the ONLY `.pl-*` classes that exist. A plausible invention
  (`.pl-datepicker`) renders as an unstyled div, so check before you use one.
- `ds_tokens` — the `--pl-*` values. Never write a literal a token already defines.
- `ds_stories` / `ds_story` — what the system already ships. If a component covers this,
  compose it rather than rebuilding it.

**Render it with `show_artifact`** — that is how a prototype reaches the operator. Pass
`kind="html"` and a fragment structured with `.pl-*` classes, or `kind="react"` to compose the
real `@pl/ui` components the artifact sandbox provides. Then call `check_artifact` and fix
anything it reports before you hand back. Say what you built and why in a sentence; the
artifact panel carries the visual, so don't paste the markup into your reply as well.

Writing the markup:
- No `<html>`, `<head>`, `<body>`, no `<style>` block. The kit stylesheet and the operator's
  theme are applied around your markup — a style block would fight them.
- Structure with `.pl-*` classes. Inline `style="…"` ONLY for layout the kit has no class for
  (a grid template, a gap), and only using `var(--pl-…)` values.
- Semantic, accessible markup: real `<button>`/`<label>`/`<nav>`, labels tied to inputs, alt text,
  a sensible heading order. This is a prototype of a component in a design system — shipping an
  inaccessible one is shipping a bug.
- Realistic content, not lorem ipsum. Plausible labels and copy make a prototype judgeable.

If the intent is already covered by an existing component, build it FROM that component and say so.
If the system genuinely lacks what's needed, compose the nearest primitives and name the gap —
a named gap is a design-system finding, and worth more than a silent one-off."""


def _designer_tools() -> list:
    """This plugin's half of the designer's toolset. It also needs `show_artifact` /
    `check_artifact` to render, which belong to the ARTIFACT plugin — named in the allowlist
    below rather than imported, because plugins coordinate through the host, never by
    importing each other (ADR 0039). An unresolved name is simply skipped, so the designer
    degrades to describing the prototype when artifact is off instead of failing."""
    return [ds_rules, ds_kit_classes, ds_tokens, ds_stories, ds_story, ds_check]


def _build_designer():
    from graph.subagents.config import SubagentConfig

    return SubagentConfig(
        name="ds-designer",
        description=(
            "Turns a design intent into a working HTML prototype built from THIS design system's "
            "real classes and tokens — for showing someone a new component, layout or styling idea "
            "before it becomes code. Returns a fragment, not a page."
        ),
        system_prompt=_DESIGNER_PROMPT,
        tools=[t.name for t in _designer_tools()] + ["show_artifact", "check_artifact"],
        max_turns=16,
    )


_WATCH_PROMPT = (
    "Design-system drift check. Call `ds_drift` to see what changed in @protolabsai/design and "
    "packages/ui since your last review. If tokens changed or components were added/removed, "
    "assess the impact on the component docs and the consuming surfaces (the marketing site, "
    "cockpit), then open a focused PR on protoContent to bring them back in sync — or, if it "
    "needs design or strategy input, write jon a tight finding. If nothing changed, do nothing."
)




# The plugin dir isn't an importable package name, so siblings load by path. One loader,
# memoised — this was copy-pasted three times before.
_SIBLINGS: dict[str, object] = {}


def _sibling(filename: str):
    """Import a sibling module by path (``theme.py`` → ``design_system_theme``)."""
    if filename not in _SIBLINGS:
        import importlib.util

        stem = Path(filename).stem
        spec = importlib.util.spec_from_file_location(f"design_system_{stem}", Path(__file__).resolve().parent / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SIBLINGS[filename] = mod
    return _SIBLINGS[filename]


def _theme_mod():
    return _sibling("theme.py")


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


# ══ theme generation from a brand (themegen.py) ═══════════════════════════════
# Extract brand signals from a site / CSS / rendered probe / hex list, then generate a
# full dark + light theme against the LIVE token contract (every --pl-color-* var the
# DS's tokens.css defines). Pure logic in themegen.py; the fetch + file seams live here.


def _themegen_mod():
    return _sibling("themegen.py")


_TG_MAX_HTML = 2_000_000
_TG_MAX_CSS = 1_500_000
_TG_MAX_TOTAL = 5_000_000
_TG_MAX_SHEETS = 10
_TG_BUDGET_S = 30.0  # wall clock for the WHOLE extract (page + every stylesheet + redirects)


def _tg_fetch(url: str, max_bytes: int, deadline: float) -> tuple[str, str]:
    """GET ``url`` → (final_url, text) through the hardened fetcher (public hosts only, the
    connection pinned to the vetted IP, redirects re-vetted, ``deadline`` a shared wall-clock
    budget — an ABSOLUTE time.monotonic() value — decompression capped). Raises FetchError (a
    RuntimeError). The test seam for the URL path."""
    return _fetch_mod().fetch_text(url, max_bytes=max_bytes, deadline=deadline, allow_offsite_hosts=True)


def _tg_extract_url(url: str) -> dict:
    """Static read of a site: the HTML + its same-site / common-CDN stylesheets (one level of
    @import), size-capped, under ONE wall-clock deadline. No JavaScript runs."""
    import time

    tg = _themegen_mod()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    deadline = time.monotonic() + _TG_BUDGET_S
    final, html = _tg_fetch(url, _TG_MAX_HTML, deadline)
    assets = tg.site_assets(html, final)
    css_parts = [assets["inline_css"]]
    total = len(html)
    fetched, skipped, failed = [], [], []
    queue = list(assets["stylesheets"])
    seen: set[str] = set()
    while queue and len(fetched) < _TG_MAX_SHEETS:
        sheet = queue.pop(0)
        if sheet in seen:
            continue
        seen.add(sheet)
        if not tg.stylesheet_allowed(sheet, final):
            skipped.append(sheet)
            continue
        if time.monotonic() >= deadline or total > _TG_MAX_TOTAL:
            skipped.append(sheet)
            continue
        try:
            _, css = _tg_fetch(sheet, _TG_MAX_CSS, deadline)
        except Exception as e:  # noqa: BLE001 — one hostile stylesheet must not kill the extract
            failed.append(f"{sheet} ({e})")
            continue
        total += len(css)
        fetched.append(sheet)
        css_parts.append(css)
        queue.extend(u for u in tg.css_imports(css, sheet) if u not in seen)
    res = tg.extract_from_css("\n".join(css_parts), theme_color_meta=assets["theme_color"], source="url", sheets_fetched=len(fetched))
    res["url"] = final
    res["notes"].append(f"static read: HTML + {len(fetched)} stylesheet(s); {len(skipped)} skipped (off-site or over budget), {len(failed)} failed")
    res["notes"].append(
        "no JavaScript ran — for a JS-rendered site (SPA, CSS-in-JS) a RENDERED probe is far more "
        "accurate: call theme_probe_script, run it with browser_eval on the page, pass the JSON here"
    )
    return res


@tool
def theme_extract(source: str) -> str:
    """Pull a brand's colors, fonts and radius out of a site or brand material — the first step
    of theming from a brand. ``source`` is ONE of:

    - a URL (``https://acme.com`` or ``acme.com``) — a STATIC read: the HTML plus its same-site
      and common-CDN stylesheets, size/time capped, no JavaScript executed;
    - raw CSS text;
    - the JSON a rendered probe returned (see ``theme_probe_script`` — best for JS-rendered sites);
    - a comma list of brand hex colors (``#0f766e, #f59e0b``) — first is primary.

    Returns a RANKED report: brand color candidates, each with its score and the evidence it came
    from (which property/selector/custom property), neutrals, the likely ground (dark or light
    site) and text color, font stacks, radius mode, and SUGGESTED SEEDS with a confidence. If the
    confidence is not "high", confirm the primary with the operator before ``theme_generate``."""
    tg = _themegen_mod()
    s = (source or "").strip()
    if not s:
        return "theme_extract: pass a URL, CSS text, a probe JSON, or a comma list of hex colors."
    kind = tg.classify_source(s)
    try:
        if kind == "url":
            res = _tg_extract_url(s)
        elif kind == "probe":
            res = tg.extract_from_computed(s)
        elif kind == "colors":
            res = tg.extract_from_colors(s)
        else:
            res = tg.extract_from_css(s)
    except RuntimeError as e:
        return f"theme_extract error: {e}"
    except (ValueError, json.JSONDecodeError) as e:
        return f"theme_extract error: could not read the {kind} input ({e})"
    return tg.format_extraction(res)


@tool
def theme_probe_script() -> str:
    """The in-page probe for a RENDERED brand read — better than a static URL read for
    JS-rendered sites (SPAs, CSS-in-JS, Tailwind JIT). Workflow: open the site with the browser
    tools (``browser_open``), run the returned expression with ``browser_eval``, then pass the
    JSON string it returns to ``theme_extract``. It samples visible elements' computed colors
    weighted by RENDERED area, the :root custom properties, fonts and radii, and stays < ~20KB."""
    tg = _themegen_mod()
    return (
        "Run this with browser_eval on the target page (after browser_open), then call "
        "theme_extract(source=<the JSON string it returns>):\n\n" + tg.PROBE_JS
    )


def _themes_dir() -> Path:
    """Generated themes live in the plugin's instance data dir, under ``themes/``."""
    d = _data_dir() / "themes"
    d.mkdir(parents=True, exist_ok=True)
    return d


_THEME_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
THEME_PREVIEW_ROUTE = "/plugins/design-system/themes/{name}/preview"


@tool
def theme_generate(primary: str, secondary: str = "", neutral: str = "", name: str = "brand", scope: str = ":root", font_family: str = "", radius: str = "") -> str:
    """Generate a complete DARK + LIGHT theme from brand seeds, against the design system's LIVE
    token contract: every ``--pl-color-*`` var the DS's tokens.css defines gets a value in both
    themes (surfaces keep the DS's elevation relation, neutrals are tinted toward the brand hue,
    the accent is stepped per theme until it clears AA, status colors are harmonized but kept
    recognizable, chart series are rotated onto the brand and kept distinguishable). Every
    text/ground pair is WCAG-checked (4.5:1 text, 3:1 tertiary/UI) and repaired by nudging
    lightness — each repair is reported.

    Args: ``primary`` (required, any opaque CSS color — usually theme_extract's suggested
    primary); ``secondary`` (optional second brand family); ``neutral`` (optional gray whose hue
    tints surfaces); ``name`` (letters/digits/-/_); ``scope`` — ``:root`` to replace the default
    theme, or a selector such as ``[data-brand="acme"]`` (comma lists allowed) for a white-label
    scope; ``font_family`` (a plain font stack — names, commas, quotes only) / ``radius`` (a
    number, px or rem) override the DS's --pl-font-sans / --pl-radius.

    Returns: key tokens, the contrast summary, every adjustment/repair, then THREE hand-offs —
    (1) a compact HTML preview between ``<<<ARTIFACT_HTML`` / ``ARTIFACT_HTML>>>`` to pass
    verbatim as ``show_artifact(kind="html", code=…)`` (it styles itself with the DS kit the
    artifact sandbox loads); (2) ``theme_apply`` maps (``dark`` / ``light`` JSON, colors + font/
    radius) to pass as ``overrides_json``; (3) the full preview's console URL (with the contrast
    table) for the operator to open. Also writes ``<name>.theme.css`` (the DS's 4-block shape;
    load AFTER tokens.css), ``<name>.theme.json`` and ``<name>.preview.html`` to the plugin's
    instance data dir, for a PR into a consumer repo."""
    tg, tk = _themegen_mod(), _tokens_mod()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name or "brand").strip("-")[:48] or "brand"
    try:
        seeds = tg.seeds_from_brand(primary, secondary, neutral)
    except ValueError as e:
        return f"theme_generate error: {e}"
    try:
        contract = tk.parse_css(_gh_get_raw(_cfg("tokens_css_path")))
    except RuntimeError as e:
        return f"theme_generate error: could not read the token contract — {e}"
    try:
        theme = tg.generate(seeds, contract, name=safe, font_family=font_family, radius=radius)
        css = tg.render_css(theme, scope)
    except ValueError as e:
        return f"theme_generate error: {e}"
    d = _themes_dir()
    paths = {"css": d / f"{safe}.theme.css", "json": d / f"{safe}.theme.json", "preview": d / f"{safe}.preview.html"}
    try:
        paths["css"].write_text(css, encoding="utf-8")
        paths["json"].write_text(tg.render_json(theme), encoding="utf-8")
        paths["preview"].write_text(tg.preview_html(theme), encoding="utf-8")
    except OSError as e:
        return f"theme_generate error: could not write the theme files ({e})"

    lines = [f"Theme '{safe}' — {len(theme['dark'])} --pl-color-* vars × dark + light, scope {scope}"]
    lines.append(f"seeds: primary {seeds['primary']}" + (f", secondary {seeds['secondary']}" if seeds["secondary"] else "") + (f", neutral {seeds['neutral']}" if seeds["neutral"] else ""))
    lines.append("\nKey tokens (dark | light):")
    for short, dv, lv in tg.summary_tokens(theme):
        lines.append(f"  {short:<16} {dv:<24} {lv}")
    if theme["base"]:
        lines.append("  " + ", ".join(f"{k}: {v}" for k, v in theme["base"].items()))
    lines.append("\nContrast:\n" + tg.contrast_report(theme, only_notable=True))
    fixes = theme["adjustments"] + theme["repairs"]
    if fixes:
        lines.append("\nAdjustments & repairs:")
        for r in fixes:
            lines.append(f"  {r['theme']} {str(r['var']).removeprefix('--pl-color-')}: {r['from']} → {r['to']} — {r['reason']}")
    for n in theme.get("notes", []):
        lines.append(f"note: {n}")
    if theme["passthrough"]:
        lines.append("left at DS default (not a literal color): " + ", ".join(theme["passthrough"]))
    lines.append(
        "\n1) SHOW IT — call show_artifact(kind=\"html\", title=\"" + safe + " theme\", code=<everything between the markers>):"
    )
    lines.append("<<<ARTIFACT_HTML\n" + tg.preview_html(theme, compact=True) + "\nARTIFACT_HTML>>>")
    apply_maps = tg.apply_maps(theme)
    lines.append("\n2) APPLY TO THIS CONSOLE — theme_apply(overrides_json=<one map>, mode=\"dark\"|\"light\"):")
    lines.append("theme_apply dark: " + json.dumps(apply_maps["dark"], separators=(",", ":")))
    lines.append("theme_apply light: " + json.dumps(apply_maps["light"], separators=(",", ":")))
    lines.append(
        f"\n3) FULL PREVIEW with the contrast table (open in the console's browser): {THEME_PREVIEW_ROUTE.format(name=safe)}"
    )
    lines.append("Files (for a PR into a consumer repo — load the .theme.css AFTER tokens.css):\n" + "\n".join(f"  {k}: {p}" for k, p in paths.items()))
    return "\n".join(lines)


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

    @router.get("/themes/{name}/preview", include_in_schema=False)
    def theme_preview(name: str) -> HTMLResponse:
        """A generated theme's full preview — public like the view page (an iframe/tab
        navigation carries no bearer). The name is allowlisted, so no path can escape the
        themes dir; the page holds only colors, and is served with a CSP that runs no script."""
        if not _THEME_NAME_RE.match(name or ""):
            return HTMLResponse("bad theme name", status_code=400)
        f = _themes_dir() / f"{name}.preview.html"
        if not f.is_file():
            return HTMLResponse(f"no generated theme named {name!r} — run theme_generate first", status_code=404)
        return HTMLResponse(
            f.read_text(encoding="utf-8"),
            headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"},
        )

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
        """Drop every cached read so a DS deploy shows up without waiting out the TTL.

        Both caches, deliberately: clearing only the Storybook one left ds_kit_classes serving
        a stale class vocabulary with no way to refresh it, so a prototype could be written
        against classes the kit no longer ships.
        """
        global _SB_CACHE, _KIT_CACHE, _VOCAB_CACHE, _INV_CACHE
        _SB_CACHE = None
        _KIT_CACHE = None
        _VOCAB_CACHE = None
        _INV_CACHE = None
        return {"ok": True, "cleared": ["storybook", "kit-classes", "vocabulary", "inventory"]}

    return router


def _parse_roots(value) -> list[str]:
    """``audit_roots`` arrives as a list (YAML / string_list setting) or a newline/comma string."""
    if not value:
        return []
    if isinstance(value, str):
        value = re.split(r"[\n,]", value)
    return [str(v).strip() for v in value if str(v).strip()]


def register(registry) -> None:
    global _EMIT, _AUDIT_ROOTS
    cfg = registry.config or {}
    _EMIT = getattr(registry, "emit", None)
    for k in _DEFAULTS:
        # A key PRESENT in the config is recorded verbatim — a blank included — so an operator
        # who clears storybook_url / watch_cron actually disables it (see _cfg). Only a key the
        # config omits keeps the default already sitting in _CFG.
        if k in cfg:
            v = cfg.get(k)
            _CFG[k] = "" if v is None else str(v)
    _AUDIT_ROOTS = _parse_roots(cfg.get("audit_roots"))
    # View PAGE: public /plugins/design-system (ungated) — iframe nav carries no bearer.
    registry.register_router(_build_view_router(), prefix="/plugins/design-system")
    # DATA: gated /api/plugins/design-system — fetched with the handshake token.
    registry.register_router(_build_data_router(), prefix="/api/plugins/design-system")
    tools = [ds_tokens, ds_components, ds_component, ds_stories, ds_story, ds_search, ds_kit_classes, ds_rules, ds_check, ds_audit_repo,
             ds_site_probe_script, ds_audit_url, ds_component_gaps, ds_drift, theme_scale, theme_contrast, theme_palette, theme_apply,
             theme_extract, theme_probe_script, theme_generate]
    registry.register_tools(tools)

    # design-critic subagent (ADR 0018) — reviews a prototype/component against the LIVE DS + a11y,
    # grounded via the ds_* tools above. The lead delegates to it with `task("design-critic", …)`.
    for build in (_build_design_critic, _build_explainer, _build_designer):
        try:
            registry.register_subagent(build())
        except Exception:  # noqa: BLE001 — a registry hiccup must not break plugin load
            log.exception("[design-system] failed to register a subagent (%s)", build.__name__)

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

    log.info("[design-system] registered %d tools + design-critic/ds-explainer/ds-designer subagents (repo=%s@%s, drift-watch=%s)", len(tools), _cfg("repo"), _cfg("ref"), cron or "off")
