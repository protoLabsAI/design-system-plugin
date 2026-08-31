"""Design-token parsing — reads the GENERATED ``tokens.css``, not the source JSON.

Why the CSS and not ``tokens.json``: consumers write ``var(--pl-color-brand-lavender)``,
and that name is produced by the DS's own build (a camelCase→kebab fold over the token
path). Re-deriving the rule here would mean maintaining a second copy of it, and a token
name this plugin invents but the DS doesn't publish is worse than no name at all — the
agent would confidently hand someone a var that resolves to nothing.

So: the JSON is the values, the CSS is the CONTRACT. We parse the CSS.

Pure logic (no protoAgent imports, no network); ``__init__`` owns the fetch.
"""

from __future__ import annotations

import re

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_BLOCK_RE = re.compile(r"(?P<sel>[^{}]+)\{(?P<body>[^{}]*)\}", re.S)
_DECL_RE = re.compile(r"(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);")
# The light-preference wrapper, with its whole (single-nesting) body.
_LIGHT_MEDIA_RE = re.compile(r"@media[^{]*prefers-color-scheme\s*:\s*light[^{]*\{(?P<body>.*?)\n\}", re.S)

_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|oklch\(|color\()")
_LENGTH_RE = re.compile(r"^-?[\d.]+(px|rem|em|%|ch|vh|vw)$")
_DURATION_RE = re.compile(r"^[\d.]+m?s$")
_NUMBER_RE = re.compile(r"^-?[\d.]+$")

# Prefix → the section a token belongs to, in the order a foundations page should read:
# what things are made of (color) before how they're arranged (space) before how they move.
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("--pl-color-", "Color"),
    ("--pl-gradient-", "Gradient"),
    ("--pl-shadow-", "Elevation"),
    ("--pl-font-", "Typography"),
    ("--pl-radius", "Radius"),
    ("--pl-border-width", "Border"),
    ("--pl-space-", "Space"),
    ("--pl-motion-", "Motion"),
)


def classify(value: str) -> str:
    """What KIND of thing a token value is — drives how the pane renders it.

    Ordering matters: a gradient contains color stops and a shadow contains an rgba(),
    so both must be checked before the generic color test.
    """
    v = (value or "").strip()
    if "gradient(" in v:
        return "gradient"
    if v.count(" ") >= 2 and ("rgba(" in v or "rgb(" in v) and ("px" in v):
        return "shadow"
    if _COLOR_RE.match(v):
        return "color"
    if _DURATION_RE.match(v):
        return "duration"
    if _LENGTH_RE.match(v):
        return "length"
    if "," in v and ('"' in v or "serif" in v or "monospace" in v or "system-ui" in v):
        return "font"
    if _NUMBER_RE.match(v):
        return "number"
    return "keyword"


def _parse_block(body: str) -> dict[str, str]:
    return {name: val.strip() for name, val in _DECL_RE.findall(body)}


def _blocks(css: str) -> list[tuple[str, dict[str, str]]]:
    """(normalized selector, declarations) for each rule, comments stripped.

    Comments are removed FIRST because the DS emits a leading ``/* GENERATED … */`` and a
    ``/* Explicit theme force … */`` that would otherwise ride along inside the captured
    selector text and break exact selector matching.
    """
    out = []
    for m in _BLOCK_RE.finditer(_COMMENT_RE.sub(" ", css or "")):
        decls = _parse_block(m.group("body"))
        if decls:
            out.append((" ".join(m.group("sel").split()), decls))
    return out


def parse_css(css: str) -> dict[str, dict[str, str]]:
    """Split ``tokens.css`` into ``{"dark": {var: value}, "light": {var: value}}``.

    Resolved STRUCTURALLY, never by source order. The naive read — walk every ``:root``
    block in sequence — is wrong in a way that hides: the ``@media (prefers-color-scheme:
    light)`` wrapper contains its own inner ``:root``, so a flat scan folds light values
    into the dark set and only looks correct while a later ``[data-theme="dark"]`` block
    happens to overwrite them again. Reorder the DS's output and dark silently becomes
    light. So the media wrapper is lifted out by name before anything else is read.

    The bare ``:root`` block carries the FULL token set (the DS ships dark as its base);
    themed blocks carry only deltas, so each theme is the base overlaid with its own
    overrides — never an override block alone, which would drop every unchanged token.
    """
    text = css or ""
    light_media: dict[str, str] = {}
    for m in _LIGHT_MEDIA_RE.finditer(text):
        for _, decls in _blocks(m.group("body")):
            light_media.update(decls)
    outer = _LIGHT_MEDIA_RE.sub(" ", text)

    base: dict[str, str] = {}
    dark_over: dict[str, str] = {}
    light_over: dict[str, str] = {}
    for sel, decls in _blocks(outer):
        if 'data-theme="light"' in sel:
            light_over.update(decls)
        elif 'data-theme="dark"' in sel:
            dark_over.update(decls)
        elif sel.endswith(":root"):
            base.update(decls)
    return {
        "dark": {**base, **dark_over},
        "light": {**base, **light_media, **light_over},
    }


def section_of(var: str) -> str:
    for prefix, label in _SECTIONS:
        if var.startswith(prefix):
            return label
    return "Other"


def group_tokens(dark: dict[str, str], light: dict[str, str] | None = None) -> list[dict]:
    """Fold the flat var map into ordered sections of renderable token records.

    Each record carries BOTH theme values so the pane can show a token's light and dark
    face together — the pair is the thing a designer actually needs to judge, and a
    single-theme swatch is how contrast regressions ship unnoticed.
    """
    light = light or {}
    order = {label: i for i, (_, label) in enumerate(_SECTIONS)}
    buckets: dict[str, list[dict]] = {}
    for var, value in dark.items():
        section = section_of(var)
        buckets.setdefault(section, []).append(
            {
                "var": var,
                "name": var.removeprefix("--pl-"),
                "value": value,
                "light": light.get(var, value),
                "kind": classify(value),
                "themed": light.get(var, value) != value,
            }
        )
    return [
        {"section": s, "tokens": buckets[s]}
        for s in sorted(buckets, key=lambda s: (order.get(s, len(order)), s))
    ]


def summarize(sections: list[dict]) -> str:
    """Compact token vocabulary for the agent — every var name with its value.

    This is the list the agent checks a literal against, so it must be COMPLETE; the
    cost is bounded (a design system has ~100 tokens, not thousands).
    """
    lines: list[str] = []
    total = 0
    for sec in sections:
        lines.append(f"\n{sec['section']}")
        for t in sec["tokens"]:
            total += 1
            themed = f"   (light: {t['light']})" if t["themed"] else ""
            lines.append(f"  var({t['var']}) = {t['value']}{themed}")
    return f"{total} tokens\n" + "\n".join(lines)
