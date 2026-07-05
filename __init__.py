"""design-system plugin (ADR 0027) — the agent's LIVE window into the protoLabs.studio
design system.

The design system is a live source of truth: ``@protolabsai/design`` owns the brand values
(``src/tokens.js`` → built ``dist/tokens.json`` → ``--pl-*`` CSS vars + a Tailwind preset),
``packages/ui`` owns the components, and ``docs/reference/visual-identity.md`` owns the rules.
This plugin reads them STRAIGHT FROM the repo at call time so the agent works from the current
vocabulary — never a stale copy frozen into its knowledge base (the anti-drift principle).

Tools:
  ds_tokens      — the live token vocabulary (colors, spacing, radius, type, …)
  ds_components  — the component inventory (packages/ui)
  ds_component   — one component's Storybook story (variants / API / usage)
  ds_rules       — the visual-identity rules (when to use what, what we don't do)
  ds_check       — lint code: flag hardcoded hex a token already defines
  ds_drift       — what changed since the last check (tokens + components); updates a snapshot

A recurring DRIFT WATCH (native scheduler) fires a turn on a cadence that calls ds_drift and,
if the DS moved, has the agent sync docs/consumers (a PR) or hand the lead a finding.
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
    "components_path": "packages/ui/src",
    "rules_path": "docs/reference/visual-identity.md",
    "watch_cron": "0 14 * * *",
}
# Register-time config snapshot (populated from registry.config in register(); rebuilt on a
# config reload). Process-local — each fleet member runs its own server process.
_CFG: dict[str, str] = dict(_DEFAULTS)


def _cfg(key: str) -> str:
    return str(_CFG.get(key) or _DEFAULTS.get(key) or "")


# ── GitHub contents API (token-authed; protoContent is private) ───────────────


def _headers(accept: str) -> dict[str, str]:
    h = {"Accept": accept, "User-Agent": "protoagent-design-system"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
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
    """Return the LIVE protoLabs.studio design tokens — the current source-of-truth vocabulary
    (colors, spacing, radius, typography, …) from @protolabsai/design. Read this before writing
    ANY styling; never hardcode a value a token already defines. Fetched live from the repo."""
    try:
        raw = _gh_get_raw(_cfg("tokens_path"))
    except RuntimeError as e:
        return f"ds_tokens error: {e}"
    try:
        return "Live design tokens (@protolabsai/design) — use these, never a literal:\n" + json.dumps(json.loads(raw), indent=1)
    except json.JSONDecodeError:
        return raw  # not JSON (e.g. a src fallback) — hand the raw file to the agent


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


_WATCH_PROMPT = (
    "Design-system drift check. Call `ds_drift` to see what changed in @protolabsai/design and "
    "packages/ui since your last review. If tokens changed or components were added/removed, "
    "assess the impact on the component docs and the consuming surfaces (the marketing site, "
    "cockpit), then open a focused PR on protoContent to bring them back in sync — or, if it "
    "needs design or strategy input, write jon a tight finding. If nothing changed, do nothing."
)


def register(registry) -> None:
    cfg = registry.config or {}
    for k in _DEFAULTS:
        v = cfg.get(k)
        if v not in (None, ""):
            _CFG[k] = str(v)
    registry.register_tools([ds_tokens, ds_components, ds_component, ds_rules, ds_check, ds_drift])

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

    log.info("[design-system] registered 6 tools (repo=%s@%s, drift-watch=%s)", _cfg("repo"), _cfg("ref"), cron or "off")
