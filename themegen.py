"""Theme generation from a brand — extract brand signals from a site, then generate a full
dark + light theme against the design system's LIVE token contract.

Two halves:

* **Extraction** — ``extract_from_css`` (static CSS text), ``extract_from_computed`` (the JSON a
  rendered-page probe returns; ``PROBE_JS`` is that probe), ``site_assets`` (an HTML page → its
  inline CSS, stylesheet URLs and ``theme-color`` meta). All return the SAME shape: ranked brand
  candidates with the evidence each came from, plus likely ground/text, fonts and radius — not a
  single guess, because "what is this brand's color" is often ambiguous and the operator should
  be able to see why a candidate ranked where it did.

* **Generation** — ``generate(seeds, contract)`` where ``contract`` is ``tokens.parse_css`` output
  (``{"dark": {var: value}, "light": {...}}``). The var list is NEVER hardcoded: every
  ``--pl-color-*`` the contract defines gets a value, its role inferred from its NAME (a role
  table with a hue-shift fallback) and its lightness structure taken from the DS's OWN default
  value in each theme — so the DS's elevation relation (dark: raised lighter than the ground;
  light: raised is the lightest) and its text tiers carry over by construction. Every text/ground
  pair is then contrast-checked and REPAIRED by nudging LCH lightness, with each repair reported.

Pure logic — no protoAgent imports, no network. Color math via the in-repo ``colorcore`` (loaded
by path, like ``theme.py``); tier choice via ``theme.scale``.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import math
import re
from pathlib import Path as _Path
from urllib.parse import urljoin, urlparse


def _load(stem: str, filename: str):
    spec = _ilu.spec_from_file_location(stem, _Path(__file__).resolve().parent / filename)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load("design_system_colorcore_tg", "colorcore.py")
_theme = _load("design_system_theme_tg", "theme.py")

# ══════════════════════════════════════════════════════════════════════════════
# CSS color parsing — the contract ships hex, rgba() and oklch(); sites ship anything.
# ══════════════════════════════════════════════════════════════════════════════

_NAMED = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000", "blue": "#0000ff",
    "yellow": "#ffff00", "orange": "#ffa500", "purple": "#800080", "navy": "#000080", "teal": "#008080",
    "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0", "maroon": "#800000", "olive": "#808000",
    "lime": "#00ff00", "aqua": "#00ffff", "cyan": "#00ffff", "fuchsia": "#ff00ff", "magenta": "#ff00ff",
    "pink": "#ffc0cb", "gold": "#ffd700", "indigo": "#4b0082", "violet": "#ee82ee", "crimson": "#dc143c",
    "coral": "#ff7f50", "tomato": "#ff6347", "salmon": "#fa8072", "tan": "#d2b48c", "beige": "#f5f5dc",
    "whitesmoke": "#f5f5f5", "gainsboro": "#dcdcdc", "lightgray": "#d3d3d3", "darkgray": "#a9a9a9",
    "dimgray": "#696969", "slategray": "#708090", "royalblue": "#4169e1", "dodgerblue": "#1e90ff",
    "steelblue": "#4682b4", "rebeccapurple": "#663399", "orangered": "#ff4500", "seagreen": "#2e8b57",
}
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?"
_FUNC_RE = re.compile(r"^(rgba?|hsla?|oklch|oklab|lab|lch|color)\(\s*(.*?)\s*\)$", re.IGNORECASE | re.DOTALL)
COLOR_TOKEN_RE = re.compile(
    r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{4}\b|#[0-9a-fA-F]{3}\b"
    r"|(?:rgba?|hsla?|oklch|oklab|lab|lch|color)\([^()]*\)",
    re.IGNORECASE,
)
_NAMED_TOKEN_RE = re.compile(r"(?<![-\w#])(" + "|".join(sorted(_NAMED, key=len, reverse=True)) + r")(?![-\w])", re.IGNORECASE)


def _num(tok: str, pct_scale: float = 1.0) -> float:
    tok = tok.strip().lower()
    if tok in ("none", ""):
        return 0.0
    if tok.endswith("%"):
        return float(tok[:-1]) / 100.0 * pct_scale
    for unit, mul in (("deg", 1.0), ("turn", 360.0), ("rad", 180.0 / math.pi), ("grad", 0.9)):
        if tok.endswith(unit):
            return float(tok[: -len(unit)]) * mul
    return float(tok)


def _split_args(body: str, n: int = 3) -> tuple[list[str], str | None]:
    alpha = None
    if "/" in body:
        body, alpha = body.split("/", 1)
    parts = [p for p in re.split(r"[\s,]+", body.strip()) if p]
    if alpha is None and len(parts) == n + 1:
        alpha = parts.pop()
    return parts, (alpha.strip() if alpha else None)


def _oklab_to_rgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    lin = (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )
    return tuple(cc._linear_to_srgb(max(0.0, min(1.0, c))) for c in lin)  # type: ignore[return-value]


def _hsl_to_rgb(h: float, s: float, l: float) -> tuple[float, float, float]:
    h = (h % 360) / 360.0

    def hue(p, q, t):
        t %= 1.0
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    if s == 0:
        return (l, l, l)
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    return (hue(p, q, h + 1 / 3), hue(p, q, h), hue(p, q, h - 1 / 3))


def parse_color(value: str) -> tuple[tuple[float, float, float], float] | None:
    """Any CSS color literal → ``((r, g, b) 0..1, alpha)``; ``None`` for things that aren't a
    literal color (``var(…)``, ``currentColor``, gradients, garbage). Handles hex (3/4/6/8),
    rgb[a], hsl[a], oklch, oklab, lab/lch (approximate — CSS lab is D50, colorcore is D65),
    ``color(srgb …)`` and the common named colors."""
    v = (value or "").strip().lower().replace("!important", "").strip()
    if not v:
        return None
    if v == "transparent":
        return ((0.0, 0.0, 0.0), 0.0)
    if v in _NAMED:
        return (cc.parse_hex(_NAMED[v]), 1.0)
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8) or not re.fullmatch(r"[0-9a-f]+", h):
            return None
        rgb = tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
        return (rgb, int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0)  # type: ignore[return-value]
    m = _FUNC_RE.match(v)
    if not m:
        return None
    fn, body = m.group(1), m.group(2)
    try:
        parts, alpha_tok = _split_args(body, 4 if fn == "color" else 3)
        alpha = _num(alpha_tok) if alpha_tok else 1.0
        if fn.startswith("rgb"):
            rgb = tuple(_num(p, 255.0) / 255.0 for p in parts[:3])
        elif fn.startswith("hsl"):
            rgb = _hsl_to_rgb(_num(parts[0]), _num(parts[1]), _num(parts[2]))
        elif fn == "oklch":
            L = _num(parts[0])  # a % folds to 0..1 in _num
            C = _num(parts[1], 0.4)
            h = _num(parts[2])
            rgb = _oklab_to_rgb(L, C * math.cos(math.radians(h)), C * math.sin(math.radians(h)))
        elif fn == "oklab":
            rgb = _oklab_to_rgb(_num(parts[0]), _num(parts[1], 0.4), _num(parts[2], 0.4))
        elif fn == "lab":
            L, a, b = _num(parts[0], 100.0), _num(parts[1], 125.0), _num(parts[2], 125.0)
            rgb = cc.fit_lch((L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360))
        elif fn == "lch":
            rgb = cc.fit_lch((_num(parts[0], 100.0), _num(parts[1], 150.0), _num(parts[2])))
        else:  # color(srgb r g b)
            if not parts or parts[0] not in ("srgb", "srgb-linear", "display-p3"):
                return None
            rgb = tuple(_num(p) for p in parts[1:4])
            if parts[0] == "srgb-linear":
                rgb = tuple(cc._linear_to_srgb(c) for c in rgb)
        if len(rgb) != 3:
            return None
        rgb = tuple(max(0.0, min(1.0, c)) for c in rgb)
        return (rgb, max(0.0, min(1.0, alpha)))  # type: ignore[return-value]
    except (ValueError, IndexError):
        return None


def to_css(rgb, alpha: float = 1.0) -> str:
    """``#rrggbb`` when opaque, ``rgba(r, g, b, a)`` otherwise — the contract's own two forms."""
    if alpha >= 0.999:
        return cc.to_hex(rgb)
    r, g, b = (round(max(0.0, min(1.0, c)) * 255) for c in rgb)
    return f"rgba({r}, {g}, {b}, {round(alpha, 3):g})"


def _hex(value: str) -> str | None:
    p = parse_color(value)
    return cc.to_hex(p[0]) if p else None


def _lch(hexv: str) -> tuple[float, float, float]:
    return cc.hex_to_lch(hexv)


def _from_lch(L: float, C: float, h: float) -> str:
    return cc.lch_to_hex((max(0.0, min(100.0, L)), max(0.0, C), h % 360))


def ratio(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio between two opaque colors (any CSS literal)."""
    la = cc.wcag_luminance(parse_color(a)[0])
    lb = cc.wcag_luminance(parse_color(b)[0])
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _hue_delta(frm: float, to: float) -> float:
    """Shortest signed arc from ``frm`` to ``to`` in degrees."""
    return ((to - frm + 180) % 360) - 180


def _delta_e(h1: str, h2: str) -> float:
    """CIE76 ΔE between two hex colors (via colorcore's LCH)."""
    (l1, c1, a1), (l2, c2, a2) = _lch(h1), _lch(h2)
    x1, y1 = c1 * math.cos(math.radians(a1)), c1 * math.sin(math.radians(a1))
    x2, y2 = c2 * math.cos(math.radians(a2)), c2 * math.sin(math.radians(a2))
    return math.sqrt((l1 - l2) ** 2 + (x1 - x2) ** 2 + (y1 - y2) ** 2)


# ══════════════════════════════════════════════════════════════════════════════
# Extraction
# ══════════════════════════════════════════════════════════════════════════════

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_VAR_RE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)")

_BRAND_PROP_RE = re.compile(r"brand|primary|accent|main-color|theme-color|highlight|cta|link|secondary", re.IGNORECASE)
_BG_PROP_RE = re.compile(r"(^|-)(bg|background|surface|canvas|page|base-100|body-bg|backdrop)(-|$)", re.IGNORECASE)
_FG_PROP_RE = re.compile(r"(^|-)(fg|foreground|text|ink|body-color|content|on-background)(-|$)", re.IGNORECASE)
_FONT_PROP_RE = re.compile(r"font", re.IGNORECASE)
# A brand-NAMED var that is really a status/text/line role (`--brand-color-danger-fg`,
# `--primary-text`, `--accent-border`) says nothing about the brand hue.
_NOT_BRAND_PROP_RE = re.compile(
    r"danger|error|success|warning|warn|info|critical|attention|severe|negative|positive|"
    r"(^|-)(fg|text|muted|subtle|border|shadow|disabled|placeholder|neutral|gr[ae]y|on)(-|$)|"
    r"(fg|text|border|shadow)color",  # camelCase design-token names (`--fgColor-accent`)
    re.IGNORECASE,
)


def is_brand_prop(name: str) -> bool:
    """A custom property whose NAME claims the brand hue (and isn't a status/text/line role)."""
    n = name.lstrip("-")
    return bool(_BRAND_PROP_RE.search(n)) and not _NOT_BRAND_PROP_RE.search(n) and not _BG_PROP_RE.search(n)
_BRANDY_SEL_RE = re.compile(r"btn|button|cta|primary|brand|accent|(^|[\s>+~,])a(?=[\s:.\[,]|$)|\blink|nav|badge|tag|chip", re.IGNORECASE)
_ROOT_SEL_RE = re.compile(r"(^|[\s,])(html|body|:root|main|#app|#root|#__next|\.app)(?=[\s,:.\[]|$)", re.IGNORECASE)
_ICON_FONT_RE = re.compile(r"icon|awesome|material symbols|material icons|glyph|dashicons", re.IGNORECASE)

# How much a color seen on each property says about the brand/ground.
_PROP_WEIGHT = {
    "background": 3.0, "background-color": 3.0, "accent-color": 4.0,
    "color": 2.0, "fill": 1.5, "stroke": 1.0, "text-decoration-color": 1.0, "caret-color": 1.0,
    "border": 1.0, "border-color": 1.0, "border-top": 0.8, "border-bottom": 0.8, "border-left": 0.8,
    "border-right": 0.8, "border-top-color": 0.8, "border-bottom-color": 0.8, "outline": 0.5,
    "outline-color": 0.5, "box-shadow": 0.3, "--custom": 1.0,
}
_KIND = {  # property → the role bucket a color seen there counts toward
    "background": "bg", "background-color": "bg", "color": "fg", "fill": "fill", "stroke": "fill",
    "accent-color": "fill", "caret-color": "fg", "text-decoration-color": "fg",
}


class _Collector:
    """Accumulates color observations → ranked, clustered candidates with evidence."""

    def __init__(self) -> None:
        self.colors: dict[str, dict] = {}
        self.fonts: dict[str, dict] = {}
        self.radii: dict[str, int] = {}
        self.pill = 0
        self.bg_hits: list[tuple[float, str, str]] = []  # (weight, hex, evidence)
        self.fg_hits: list[tuple[float, str, str]] = []
        self.brand_props: dict[str, str] = {}
        self.theme_meta: str | None = None
        self.notes: list[str] = []
        self.sheets_fetched: int | None = None  # a URL read that got 0 stylesheets caps confidence

    def color(self, value: str, weight: float, kind: str, evidence: str, brand_bonus: float = 1.0) -> str | None:
        p = parse_color(value)
        if not p or p[1] < 0.3:
            return None
        hexv = cc.to_hex(p[0])
        rec = self.colors.setdefault(hexv, {"weight": 0.0, "brand": 0.0, "kinds": {}, "evidence": {}})
        w = weight * p[1]
        rec["weight"] += w
        rec["brand"] += w * brand_bonus
        rec["kinds"][kind] = rec["kinds"].get(kind, 0.0) + w
        rec["evidence"][evidence] = rec["evidence"].get(evidence, 0.0) + w
        return hexv

    def font(self, stack: str, weight: float, evidence: str) -> None:
        s = " ".join((stack or "").replace("!important", "").split()).strip().rstrip(";")
        if not s or s.lower() in ("inherit", "initial", "unset", "revert") or "var(" in s or _ICON_FONT_RE.search(s):
            return
        rec = self.fonts.setdefault(s, {"score": 0.0, "evidence": {}})
        rec["score"] += weight
        rec["evidence"][evidence] = rec["evidence"].get(evidence, 0.0) + weight

    def radius(self, value: str, count: int = 1) -> None:
        first = (value or "").strip().split()[0] if (value or "").strip() else ""
        m = re.fullmatch(r"(" + _NUM + r")(px|rem|em|%)?", first)
        if not m:
            return
        n, unit = float(m.group(1)), m.group(2) or "px"
        if unit in ("rem", "em"):
            n *= 16
        if unit == "%" or n >= 999:
            self.pill += count
            return
        if n <= 0 or n > 48:
            return
        key = f"{round(n):d}px"
        self.radii[key] = self.radii.get(key, 0) + count

    # ── result ──
    def result(self, source: str) -> dict:
        # Cluster near-duplicates (ΔE < 7) into the heavier member so a brand color that ships
        # as #5b3fc9 and #5c40ca doesn't split its own vote.
        items = sorted(self.colors.items(), key=lambda kv: -kv[1]["weight"])
        clusters: list[dict] = []
        for hexv, rec in items:
            for cl in clusters:
                if _delta_e(cl["hex"], hexv) < 7:
                    cl["weight"] += rec["weight"]
                    cl["brand"] += rec["brand"]
                    for k, v in rec["kinds"].items():
                        cl["kinds"][k] = cl["kinds"].get(k, 0.0) + v
                    for k, v in rec["evidence"].items():
                        cl["evidence"][k] = cl["evidence"].get(k, 0.0) + v
                    cl["members"].append(hexv)
                    break
            else:
                clusters.append({"hex": hexv, "weight": rec["weight"], "brand": rec["brand"], "kinds": dict(rec["kinds"]), "evidence": dict(rec["evidence"]), "members": [hexv]})

        def ev(cl: dict) -> list[str]:
            return [k for k, _ in sorted(cl["evidence"].items(), key=lambda kv: -kv[1])[:5]]

        background = _pick_ground(self.bg_hits, clusters, "bg")
        foreground = _pick_ground(self.fg_hits, clusters, "fg")
        ground_hexes = [g["hex"] for g in (background, foreground) if g]

        brand, neutrals = [], []
        for cl in clusters:
            L, C, h = _lch(cl["hex"])
            row = {"hex": cl["hex"], "lch": [round(L, 1), round(C, 1), round(h, 1)], "evidence": ev(cl)}
            if len(cl["members"]) > 1:
                row["merged"] = cl["members"][1:6]
            is_ground = any(_delta_e(cl["hex"], g) < 7 for g in ground_hexes)
            if C >= 12 and 4 <= L <= 97 and not is_ground:
                # Chroma factor: a saturated color on a button says "brand" louder than a dusty one.
                score = cl["brand"] * (0.45 + 0.55 * min(C, 60) / 60)
                cats = evidence_categories(cl["evidence"])
                # Mostly body text with no brand-shaped evidence = ink (a navy/slate text color),
                # not the brand.
                fg_share = cl["kinds"].get("fg", 0.0) / max(cl["weight"], 1e-9)
                if fg_share > 0.6 and not (cats & {"custom-property", "button", "theme-color"}):
                    score *= 0.2
                    row["ink"] = True
                row["score"] = round(score, 2)
                row["sources"] = sorted(cats)
                brand.append(row)
            else:
                row["score"] = round(cl["weight"], 2)
                row["role_hint"] = max(cl["kinds"], key=cl["kinds"].get) if cl["kinds"] else ""
                neutrals.append(row)
        if self.theme_meta:
            meta_hex = _hex(self.theme_meta)
            if meta_hex:
                hit = next((b for b in brand if _delta_e(b["hex"], meta_hex) < 7), None)
                if hit:
                    hit["score"] = round(hit["score"] * 1.5 + 5, 2)
                    hit["evidence"].insert(0, f'<meta name="theme-color" content="{self.theme_meta}">')
                    hit["sources"] = sorted(set(hit.get("sources", [])) | {"theme-color"})
                elif _lch(meta_hex)[1] >= 12:
                    brand.append({"hex": meta_hex, "lch": [round(x, 1) for x in _lch(meta_hex)], "score": 5.0, "evidence": [f'<meta name="theme-color" content="{self.theme_meta}">'], "sources": ["theme-color"]})
        brand.sort(key=lambda r: -r["score"])
        neutrals.sort(key=lambda r: -r["score"])

        fonts = [
            {"stack": s, "score": round(r["score"], 2), "evidence": [k for k, _ in sorted(r["evidence"].items(), key=lambda kv: -kv[1])[:3]]}
            for s, r in sorted(self.fonts.items(), key=lambda kv: -kv[1]["score"])[:6]
        ]
        radius: dict = {"values": sorted(([k, v] for k, v in self.radii.items()), key=lambda kv: -kv[1])[:6], "pill": self.pill}
        radius["mode"] = radius["values"][0][0] if radius["values"] else ""

        out = {
            "source": source,
            "brand": brand[:8],
            "neutrals": neutrals[:6],
            "background": background,
            "foreground": foreground,
            "fonts": fonts,
            "radius": radius,
            "brand_custom_props": dict(list(self.brand_props.items())[:20]),
            "theme_color_meta": self.theme_meta,
            "notes": list(self.notes),
        }
        out["suggested"] = _suggest(out, sheets_fetched=self.sheets_fetched)
        return out


def _pick_ground(hits: list[tuple[float, str, str]], clusters: list[dict], kind: str) -> dict | None:
    if hits:
        agg: dict[str, list] = {}
        for w, hexv, evid in hits:
            a = agg.setdefault(hexv, [0.0, []])
            a[0] += w
            a[1].append(evid)
        hexv, (w, evs) = max(agg.items(), key=lambda kv: kv[1][0])
    else:
        best = max((cl for cl in clusters if cl["kinds"].get(kind)), key=lambda cl: cl["kinds"][kind], default=None)
        if not best:
            return None
        hexv, evs = best["hex"], [f"most-used {kind} color"]
    L = _lch(hexv)[0]
    return {"hex": hexv, "mode": "dark" if L < 45 else "light", "L": round(L, 1), "evidence": list(dict.fromkeys(evs))[:4]}


def evidence_categories(evidence: dict | list) -> set[str]:
    """INDEPENDENT kinds of evidence behind a candidate — two selectors are one source; a
    custom property plus a button fill plus the theme-color meta are three."""
    cats: set[str] = set()
    for e in evidence:
        if "theme-color" in e:
            cats.add("theme-color")
        elif "custom property" in e:
            cats.add("custom-property")
        elif e.startswith("button-text"):
            cats.add("other")  # text sitting ON a button says nothing about the fill's brand
        elif re.search(r"\bbutton\b|btn|cta", e, re.IGNORECASE):
            cats.add("button")
        elif re.search(r"\blink\b|`a[\s:.,`\[]|nav", e, re.IGNORECASE):
            cats.add("link")
        elif "operator-supplied" in e:
            cats.add("operator")
        else:
            cats.add("other")
    return cats


CONFIDENCE_MIN_SCORE = 10.0


def _suggest(res: dict, sheets_fetched: int | None = None) -> dict:
    brand = res["brand"]
    primary = brand[0]["hex"] if brand else ""
    secondary = ""
    if brand:
        h0 = brand[0]["lch"][2]
        for b in brand[1:]:
            if b.get("ink"):  # a chromatic TEXT color (navy/slate ink) is not a second brand
                continue
            if abs(_hue_delta(h0, b["lch"][2])) >= 30 and b["score"] >= 0.25 * brand[0]["score"]:
                secondary = b["hex"]
                break
    neutral = ""
    for n in res["neutrals"]:
        if n["lch"][1] >= 2.5 and 8 < n["lch"][0] < 95:
            neutral = n["hex"]
            break
    confidence, why = "none", "no chromatic candidate"
    if brand:
        top = brand[0]
        sources = set(top.get("sources") or [])
        second = brand[1]["score"] if len(brand) > 1 else 0.0
        margin = top["score"] / max(second, 0.01)
        if "operator" in sources:
            confidence, why = "high", "operator-supplied"
        elif top["score"] >= CONFIDENCE_MIN_SCORE and len(sources - {"other"}) >= 2 and margin >= 2:
            confidence, why = "high", f"{len(sources)} independent sources, {margin:.1f}× the runner-up"
        elif top["score"] >= CONFIDENCE_MIN_SCORE / 2 and (margin >= 1.3 or sources - {"other"}):
            confidence, why = "medium", f"sources: {', '.join(sorted(sources)) or '—'}; {margin:.1f}× the runner-up"
        else:
            confidence, why = "low", f"weak signal (score {top['score']}, sources {', '.join(sorted(sources)) or '—'})"
        if sheets_fetched == 0 and confidence != "low":
            confidence, why = "low", "no stylesheet could be read — only inline styles were seen"
    return {
        "primary": primary,
        "secondary": secondary,
        "neutral": neutral,
        "site_mode": (res.get("background") or {}).get("mode", ""),
        "font_family": res["fonts"][0]["stack"] if res["fonts"] else "",
        "radius": res["radius"].get("mode", ""),
        "confidence": confidence,
        "confidence_why": why,
    }


def _css_rules(css: str):
    """(selector, [(prop, value)]) for each innermost rule, comments stripped."""
    for m in _RULE_RE.finditer(_CSS_COMMENT_RE.sub(" ", css or "")):
        sel = " ".join(m.group(1).split())
        if sel.startswith(("@font-face", "@keyframes")) or re.fullmatch(r"(from|to|[\d.]+%)(\s*,\s*(from|to|[\d.]+%))*", sel):
            continue
        decls = []
        for d in m.group(2).split(";"):
            if ":" in d:
                p, v = d.split(":", 1)
                decls.append((p.strip().lower() if not p.strip().startswith("--") else p.strip(), v.strip()))
        if decls:
            yield sel.split("{")[-1].strip(), decls


def _resolve_vars(value: str, props: dict[str, str], depth: int = 0) -> str:
    if "var(" not in value or depth > 4:
        return value

    def sub(m):
        name, fb = m.group(1), m.group(2)
        if name in props:
            return _resolve_vars(props[name], props, depth + 1)
        return fb.strip() if fb else ""

    return _VAR_RE.sub(sub, value)


def _colors_in(value: str) -> list[str]:
    found = COLOR_TOKEN_RE.findall(value)
    if not found and "gradient" not in value and "url(" not in value:
        found = _NAMED_TOKEN_RE.findall(value)
    return found


def extract_from_css(css_text: str, *, theme_color_meta: str | None = None, source: str = "css", sheets_fetched: int | None = None) -> dict:
    """Brand signals from static CSS. Returns ranked candidates with evidence::

        {source, brand:[{hex, lch, score, evidence[]}], neutrals:[…], background:{hex, mode, evidence},
         foreground:{…}, fonts:[{stack, score, evidence}], radius:{mode, values, pill},
         brand_custom_props:{}, suggested:{primary, secondary, neutral, mode_hint, font_family,
         radius, confidence}, notes:[]}

    Weighting: a color's vote depends on WHERE it appears — a background fill (3) outweighs a text
    color (2) outweighs a border (1); a brand-named custom property (``--brand``, ``--primary``,
    ``--accent``…) and brand-ish selectors (buttons, links, CTAs, nav) multiply the brand score;
    ``html``/``body``/``:root`` backgrounds decide the ground. Near-duplicates (ΔE < 7) merge.
    """
    col = _Collector()
    col.theme_meta = theme_color_meta
    col.sheets_fetched = sheets_fetched
    rules = list(_css_rules(css_text))
    # Custom properties first, so var() references resolve (root-ish scopes win ties).
    props: dict[str, str] = {}
    for sel, decls in sorted(rules, key=lambda r: 1 if _ROOT_SEL_RE.search(r[0]) else 0):
        for p, v in decls:
            if p.startswith("--"):
                props[p] = v
    for sel, decls in rules:
        rooty = bool(_ROOT_SEL_RE.search(sel)) and len(sel) < 60
        brandy = bool(_BRANDY_SEL_RE.search(sel))
        short_sel = sel if len(sel) <= 48 else sel[:45] + "…"
        for p, raw in decls:
            if p.startswith("--"):
                v = _resolve_vars(raw, props)
                cols = _colors_in(v)
                if _FONT_PROP_RE.search(p) and ("," in v or '"' in v or "'" in v) and not cols:
                    col.font(v, 2.0, f"custom property {p}")
                    continue
                if len(cols) != 1 or len(v) > 60:
                    continue
                name = p[2:]
                ev = f"custom property {p}"
                if is_brand_prop(name):
                    hexv = col.color(cols[0], 6.0, "brand-prop", ev, 1.6)
                    if hexv:
                        col.brand_props[p] = v
                elif _BG_PROP_RE.search(name):
                    hexv = col.color(cols[0], 1.5, "bg", ev, 0.6)
                    if hexv and rooty and re.search(r"(^|-)(bg|background|canvas|page|base-100|body-bg)$", name):
                        col.bg_hits.append((2.0, hexv, ev))
                elif _FG_PROP_RE.search(name):
                    hexv = col.color(cols[0], 1.0, "fg", ev, 0.6)
                    if hexv and rooty and re.search(r"(^|-)(fg|foreground|text|body-color)$", name):
                        col.fg_hits.append((2.0, hexv, ev))
                else:
                    col.color(cols[0], 0.5, "custom", ev)
                continue
            v = _resolve_vars(raw, props)
            if p in ("font-family", "font"):
                stack = v if p == "font-family" else (v.split(" ", 1)[-1] if "," in v else "")
                if p == "font" and "," in v:
                    # `font: 600 16px/1.4 "Inter", sans-serif` → the family is after the size token
                    mm = re.search(r"\d(?:px|rem|em|pt|%)?(?:/[\d.]+\w*)?\s+(.+)$", v)
                    stack = mm.group(1) if mm else ""
                if stack:
                    col.font(stack, 3.0 if rooty else 1.0, f"font-family on `{short_sel}`")
                continue
            if p in ("border-radius", "border-top-left-radius"):
                col.radius(v)
                continue
            w = _PROP_WEIGHT.get(p)
            if w is None:
                if p.startswith("border") and "color" in p:
                    w = 0.8
                elif p == "background-image" or p == "box-shadow":
                    w = 0.3
                else:
                    continue
            cols = _colors_in(v)
            if not cols:
                continue
            kind = _KIND.get(p, "line")
            bonus = 2.2 if brandy else 1.0
            for c in cols[:3]:
                hexv = col.color(c, w, kind, f"{p} on `{short_sel}`", bonus)
                # body paints over html/:root, so it decides the ground when both are set.
                rank = 4.0 if re.search(r"(^|[\s,])body\b", sel) else (3.0 if re.search(r"(^|[\s,])(html|:root)\b", sel) else 1.0)
                if hexv and rooty and p in ("background", "background-color") and len(cols) == 1:
                    col.bg_hits.append((rank, hexv, f"{p} on `{short_sel}`"))
                if hexv and rooty and p == "color" and len(cols) == 1:
                    col.fg_hits.append((rank, hexv, f"color on `{short_sel}`"))
    if not rules:
        col.notes.append("no CSS rules found in the input")
    return col.result(source)


def extract_from_computed(probe: dict | str) -> dict:
    """Brand signals from a rendered-page probe (what ``PROBE_JS`` returns). Accepted shape::

        {
          "url": str, "title": str,
          "themeColorMeta": "#hex" | null,           # <meta name="theme-color">
          "colorScheme": "dark" | "light" | "…",     # computed color-scheme on <html>
          "bodyBg": "rgb(…)", "bodyFg": "rgb(…)",     # computed body background / text color
          "colors": [{"value": "rgb(…)", "prop": "background"|"button"|"color"|"link"|
                      "button-text"|"border"|"fill", "area": px², "count": n}],
          "customProps": {"--name": "resolved value"},  # declared on :root/html/body
          "fonts": [{"family": "…", "area": px², "count": n}],
          "radii": [{"value": "6px", "count": n}]
        }

    ``area`` is RENDERED area (px²), so a hero background outvotes a 1px border — the thing a
    static CSS read can't know. Same output shape as ``extract_from_css``.
    """
    if isinstance(probe, str):
        probe = json.loads(probe)
        if isinstance(probe, str):  # a probe result that was JSON.stringify'd twice
            probe = json.loads(probe)
    if not isinstance(probe, dict):
        raise ValueError("probe must be a JSON object (the PROBE_JS result)")  # noqa: TRY004 — a tool-input error, not a caller type bug

    def as_list(key: str) -> list:
        v = probe.get(key)
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError(f"probe.{key} must be a list, got {type(v).__name__}")  # noqa: TRY004 — a tool-input error
        return [x for x in v if isinstance(x, dict)]

    def num(v, default: float = 0.0) -> float:
        if v is None or v == "":
            return default
        try:
            f = float(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"probe carries a non-numeric area/count: {v!r}") from e
        return f if math.isfinite(f) and f >= 0 else default

    props = probe.get("customProps") or {}
    if not isinstance(props, dict):
        raise ValueError(f"probe.customProps must be an object, got {type(props).__name__}")  # noqa: TRY004 — a tool-input error
    colors, fonts, radii = as_list("colors"), as_list("fonts"), as_list("radii")
    col = _Collector()
    col.theme_meta = str(probe.get("themeColorMeta") or "")[:64] or None
    prop_w = {"background": 3.0, "button": 3.0, "color": 2.0, "link": 2.0, "button-text": 1.0, "border": 0.8, "fill": 1.5}
    kind_of = {"background": "bg", "button": "bg", "color": "fg", "link": "fg", "button-text": "fg", "border": "line", "fill": "fill"}
    for c in colors[:400]:
        prop = str(c.get("prop") or "background")
        area = num(c.get("area"))
        count = num(c.get("count"), 1.0)
        w = prop_w.get(prop, 1.0) * (math.sqrt(max(area, 0)) / 40.0 + min(count, 50) * 0.5)
        bonus = 2.5 if prop in ("button", "link") else 1.0
        col.color(str(c.get("value") or ""), w, kind_of.get(prop, "line"), f"{prop} (rendered, {int(count)}×, {int(area):,}px²)", bonus)
    for name, val in list(props.items())[:800]:
        cols = _colors_in(str(val))
        if len(cols) != 1:
            if _FONT_PROP_RE.search(name) and "," in str(val):
                col.font(str(val), 2.0, f"custom property {name}")
            continue
        ev = f"custom property {name}"
        if is_brand_prop(str(name)):
            if col.color(cols[0], 8.0, "brand-prop", ev, 1.6):
                col.brand_props[name] = str(val)
        elif _BG_PROP_RE.search(name):
            col.color(cols[0], 1.0, "bg", ev, 0.5)
        else:
            col.color(cols[0], 0.5, "custom", ev)
    for key, bucket, label in (("bodyBg", col.bg_hits, "computed body background"), ("bodyFg", col.fg_hits, "computed body color")):
        p = parse_color(str(probe.get(key) or ""))
        if p and p[1] >= 0.5:
            bucket.append((5.0, cc.to_hex(p[0]), label))
    if not col.bg_hits:
        # No painted body: the page bg is the default canvas the color-scheme implies.
        dark = "dark" in str(probe.get("colorScheme") or "") and "light" not in str(probe.get("colorScheme") or "")
        col.bg_hits.append((1.0, "#121212" if dark else "#ffffff", "unpainted body → browser canvas"))
    for f in fonts[:40]:
        col.font(str(f.get("family") or "")[:300], math.sqrt(num(f.get("area"))) / 40.0 + num(f.get("count"), 1.0), "rendered text")
    for r in radii[:40]:
        col.radius(str(r.get("value") or ""), int(num(r.get("count"), 1.0)))
    res = col.result("computed")
    res["url"] = str(probe.get("url") or "")[:500]
    return res


_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))')
_STYLE_TAG_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_STYLE_ATTR_RE = re.compile(r"<(\w+)([^>]*?)\sstyle\s*=\s*(\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_IMPORT_RE = re.compile(r"@import\s+(?:url\()?\s*['\"]?([^'\")\s;]+)", re.IGNORECASE)


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(3) or m.group(4) or m.group(5) or "") for m in _ATTR_RE.finditer(tag)}


def site_assets(html: str, base_url: str) -> dict:
    """An HTML page → ``{inline_css, stylesheets:[abs urls], theme_color, imports}``. Inline
    ``style=""`` attributes become pseudo-rules (``<tag style>``) so they vote like CSS; a
    ``prefers-color-scheme: dark`` theme-color meta is skipped in favour of the default one."""
    html = html or ""
    sheets: list[str] = []
    for tag in _LINK_RE.findall(html):
        a = _attrs(tag)
        rel = a.get("rel", "").lower()
        if "stylesheet" in rel and a.get("href") and "alternate" not in rel or rel == "preload" and a.get("as") == "style" and a.get("href"):
            sheets.append(urljoin(base_url, a["href"].replace("&amp;", "&")))
    theme_color = None
    for tag in _META_RE.findall(html):
        a = _attrs(tag)
        if a.get("name", "").lower() == "theme-color" and a.get("content"):
            if "dark" in a.get("media", "") and theme_color:
                continue
            theme_color = theme_color or a["content"]
    inline = "\n".join(_STYLE_TAG_RE.findall(html))
    attr_rules = []
    for m in _STYLE_ATTR_RE.finditer(html):
        tag, rest, body = m.group(1).lower(), m.group(2), m.group(4) or m.group(5) or ""
        cls = _attrs(rest).get("class", "")
        sel = tag + ("." + ".".join(cls.split()[:2]) if cls.strip() else "") + "[style]"
        attr_rules.append(f"{sel} {{{body}}}")
    inline += "\n" + "\n".join(attr_rules[:2000])
    imports = [urljoin(base_url, u) for u in _IMPORT_RE.findall(inline)]
    return {"inline_css": inline, "stylesheets": list(dict.fromkeys(sheets + imports)), "theme_color": theme_color}


def css_imports(css: str, base_url: str) -> list[str]:
    return [urljoin(base_url, u) for u in _IMPORT_RE.findall(_CSS_COMMENT_RE.sub(" ", css or ""))]


# Stylesheet hosts theme_extract will follow off a page besides its own site.
CDN_HOSTS = (
    "fonts.googleapis.com", "cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "use.typekit.net",
    "cdn.shopify.com", "assets.squarespace.com", "static1.squarespace.com", "static.parastorage.com",
    "assets-global.website-files.com", "cdn.prod.website-files.com", "use.fontawesome.com",
)
CDN_SUFFIXES = (".cloudfront.net", ".azureedge.net", ".akamaized.net", ".fastly.net", ".website-files.com", ".wixstatic.com", ".b-cdn.net")


def _site(host: str) -> str:
    if re.fullmatch(r"[\d.]+|\[?[0-9a-fA-F:]+\]?", host or ""):
        return (host or "").lower()  # an IP literal is its own site
    parts = (host or "").lower().split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def stylesheet_allowed(sheet_url: str, page_url: str) -> bool:
    """Same site as the page (``cdn.acme.com`` for ``www.acme.com``) or a common CDN host."""
    su, pu = urlparse(sheet_url), urlparse(page_url)
    if su.scheme not in ("http", "https") or not su.hostname:
        return False
    host = su.hostname.lower()
    return _site(host) == _site(pu.hostname or "") or host in CDN_HOSTS or host.endswith(CDN_SUFFIXES)


_HEX_LIST_RE = re.compile(r"^\s*#?[0-9a-fA-F]{3,8}(\s*[,\s]\s*#?[0-9a-fA-F]{3,8})*\s*$")


def classify_source(source: str) -> str:
    """``url`` | ``probe`` | ``colors`` | ``css`` — what kind of thing theme_extract was handed."""
    s = (source or "").strip()
    if re.match(r"^https?://", s, re.IGNORECASE):
        return "url"
    if s.startswith("{") or (s.startswith('"{') and s.endswith('"')):
        return "probe"
    if _HEX_LIST_RE.match(s) and "{" not in s:
        return "colors"
    if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", s, re.IGNORECASE) and "{" not in s:
        return "url"  # bare domain
    return "css"


def extract_from_colors(text: str) -> dict:
    """A comma list of brand hex colors → the same result shape (operator-given, no ranking
    needed: first is primary, a hue-distinct second is secondary, a near-gray one is neutral)."""
    vals = [v if v.startswith("#") else "#" + v for v in re.split(r"[\s,]+", text.strip()) if v]
    col = _Collector()
    for i, v in enumerate(vals):
        col.color(v, 100.0 / (i + 1), "given", f"operator-supplied #{i + 1}")
    res = col.result("colors")
    # Operator order is authoritative: re-rank brand rows by position, not score.
    order = {cc.to_hex(cc.parse_hex(v)): i for i, v in enumerate(vals) if _hex(v)}
    res["brand"].sort(key=lambda r: order.get(r["hex"], 99))
    res["suggested"] = _suggest(res)
    if res["brand"] and not res["suggested"]["primary"]:
        res["suggested"]["primary"] = res["brand"][0]["hex"]
    if not res["brand"] and vals:
        res["suggested"]["primary"] = cc.to_hex(cc.parse_hex(vals[0]))
        res["notes"].append("no chromatic color given — the first color is used as a monochrome brand")
    return res


def format_extraction(res: dict) -> str:
    """The extraction as a compact, agent-readable report (ranked, with evidence)."""
    lines = [f"Brand extraction ({res.get('source')}{' — ' + res['url'] if res.get('url') else ''})"]
    if res["brand"]:
        lines.append("\nBrand candidates (ranked):")
        for i, b in enumerate(res["brand"], 1):
            lines.append(f"  {i}. {b['hex']}  score {b['score']}  LCH {b['lch']}")
            for e in b["evidence"][:4]:
                lines.append(f"       · {e}")
    else:
        lines.append("\nNo chromatic brand color found — the site reads as monochrome.")
    if res["neutrals"]:
        lines.append("\nNeutrals: " + ", ".join(f"{n['hex']} ({n.get('role_hint') or '?'})" for n in res["neutrals"][:6]))
    for key in ("background", "foreground"):
        g = res.get(key)
        if g:
            lines.append(f"{key.title()}: {g['hex']} ({g['mode']}, L={g['L']}) ← {'; '.join(g['evidence'][:2])}")
    if res["fonts"]:
        lines.append("Fonts: " + " | ".join(f["stack"] for f in res["fonts"][:3]))
    r = res["radius"]
    if r.get("values") or r.get("pill"):
        lines.append(f"Radius: mode {r.get('mode') or '—'}; seen {r['values'][:4]}{'; pill ×' + str(r['pill']) if r.get('pill') else ''}")
    if res.get("brand_custom_props"):
        lines.append("Brand custom properties: " + ", ".join(f"{k}: {v}" for k, v in list(res["brand_custom_props"].items())[:8]))
    s = res["suggested"]
    lines.append(
        f"\nSuggested seeds (confidence: {s['confidence']}): primary={s['primary'] or '—'} "
        f"secondary={s['secondary'] or '—'} neutral={s['neutral'] or '—'} (site is {s['site_mode'] or '?'})"
        + (f" font_family={s['font_family']!r}" if s.get("font_family") else "")
    )
    lines.append(f"Confidence reason: {s.get('confidence_why', '')}")
    if s["confidence"] in ("low", "medium", "none"):
        lines.append("Confidence is not high — CONFIRM the primary with the operator before generating.")
    for n in res.get("notes") or []:
        lines.append(f"note: {n}")
    return "\n".join(lines)


# ── the in-page probe ──────────────────────────────────────────────────────────
# A single self-contained expression (IIFE) → a JSON STRING in the extract_from_computed shape.
# Returns a string rather than an object so every eval bridge prints it verbatim.
PROBE_JS = r"""(() => {
  const out = { v: 1, url: location.href, title: document.title, themeColorMeta: null, colorScheme: "",
    bodyBg: "", bodyFg: "", colors: [], customProps: {}, fonts: [], radii: [] };
  const root = document.documentElement, body = document.body || root;
  try { const m = document.querySelector('meta[name="theme-color"]:not([media*="dark"])') || document.querySelector('meta[name="theme-color"]');
        if (m) out.themeColorMeta = m.getAttribute("content"); } catch (e) {}
  const clear = (v) => !v || v === "transparent" || /rgba\([^)]*,\s*0\)$/.test(v) || /\/\s*0\)$/.test(v);
  try {
    const rs = getComputedStyle(root), bs = getComputedStyle(body);
    out.colorScheme = rs.colorScheme || "";
    out.bodyBg = clear(bs.backgroundColor) ? (clear(rs.backgroundColor) ? "" : rs.backgroundColor) : bs.backgroundColor;
    out.bodyFg = bs.color;
  } catch (e) {}
  const colors = new Map(), fonts = new Map(), radii = new Map();
  const add = (map, key, area, extra) => { let r = map.get(key); if (!r) { r = Object.assign({ count: 0, area: 0 }, extra); map.set(key, r); } r.count++; r.area += area; };
  const vw = innerWidth || 1280, vh = innerHeight || 800, maxY = vh * 4;
  let n = 0;
  try {
    for (const el of body.querySelectorAll("*")) {
      if (n >= 3000) break;
      const tag = el.tagName;
      if (/^(SCRIPT|STYLE|META|LINK|NOSCRIPT|TEMPLATE|HEAD|TITLE|BR|WBR)$/i.test(tag)) continue;
      let r; try { r = el.getBoundingClientRect(); } catch (e) { continue; }
      if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top + scrollY > maxY) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none" || Number(cs.opacity) === 0) continue;
      n++;
      const area = Math.min(r.width * r.height, vw * vh);
      const cls = typeof el.className === "string" ? el.className : "";
      const isBtn = tag === "BUTTON" || el.getAttribute("role") === "button" || /\b(btn|button|cta)\b/i.test(cls) ||
        (tag === "INPUT" && /^(submit|button)$/i.test(el.type || ""));
      const isLink = tag === "A" && !isBtn;
      if (!clear(cs.backgroundColor)) add(colors, "bg|" + cs.backgroundColor, area, { value: cs.backgroundColor, prop: isBtn ? "button" : "background" });
      let text = 0;
      for (const c of el.childNodes) if (c.nodeType === 3) text += c.textContent.trim().length;
      if (text) {
        const fs = parseFloat(cs.fontSize) || 16, ta = Math.min(area, text * fs * fs * 0.6);
        const prop = isLink ? "link" : isBtn ? "button-text" : "color";
        add(colors, prop + "|" + cs.color, ta, { value: cs.color, prop });
        add(fonts, cs.fontFamily, ta, { family: cs.fontFamily });
      }
      if (parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== "none" && !clear(cs.borderTopColor))
        add(colors, "border|" + cs.borderTopColor, (r.width + r.height) * 2, { value: cs.borderTopColor, prop: "border" });
      if (el instanceof SVGElement && cs.fill && /^(rgb|oklch|color|#)/.test(cs.fill) && !clear(cs.fill))
        add(colors, "fill|" + cs.fill, area, { value: cs.fill, prop: "fill" });
      const rad = cs.borderTopLeftRadius;
      if (rad && rad !== "0px" && (isBtn || tag === "INPUT" || !clear(cs.backgroundColor) || parseFloat(cs.borderTopWidth) > 0))
        add(radii, rad, 1, { value: rad });
    }
  } catch (e) { out.walkError = String(e); }
  const names = new Set();
  const walk = (rules) => { for (const rule of rules) { try {
    if (rule.cssRules) walk(rule.cssRules);
    if (rule.style && /(^|,)\s*(:root|html|body)\b/.test(rule.selectorText || ""))
      for (const p of rule.style) if (p.startsWith("--") && names.size < 600) names.add(p);
  } catch (e) {} } };
  for (const sh of Array.from(document.styleSheets)) {
    try { walk(sh.cssRules); } catch (e) { out.crossOriginSheets = (out.crossOriginSheets || 0) + 1; }
  }
  try {
    const rs = getComputedStyle(root);
    for (const p of names) { const v = rs.getPropertyValue(p).trim(); if (v && v.length < 120) out.customProps[p] = v; }
  } catch (e) {}
  const top = (map, k) => Array.from(map.values()).sort((a, b) => b.area - a.area).slice(0, k)
    .map((r) => Object.assign({}, r, { area: Math.round(r.area) }));
  out.colors = top(colors, 80);
  out.fonts = top(fonts, 8);
  out.radii = Array.from(radii.values()).sort((a, b) => b.count - a.count).slice(0, 10).map((r) => ({ value: r.value, count: r.count }));
  let s = JSON.stringify(out);
  if (s.length > 19000) {
    const keep = {}, re = /brand|primary|accent|secondary|color|bg|background|fg|text|surface|font|radius/i;
    for (const [k, v] of Object.entries(out.customProps)) if (re.test(k) && Object.keys(keep).length < 120) keep[k] = v;
    out.customProps = keep; s = JSON.stringify(out);
  }
  while (s.length > 19000 && out.colors.length > 20) { out.colors = out.colors.slice(0, out.colors.length - 10); s = JSON.stringify(out); }
  if (s.length > 19000) { out.customProps = {}; s = JSON.stringify(out); }
  return s;
})()"""


# ══════════════════════════════════════════════════════════════════════════════
# Seeds
# ══════════════════════════════════════════════════════════════════════════════


def seeds_from_brand(primary: str, secondary: str = "", neutral: str = "") -> dict:
    """Normalize operator-given brand colors into a seeds dict for ``generate``. Any OPAQUE CSS
    color literal is accepted; raises ``ValueError`` on an unparseable or translucent one (a
    brand seed with alpha has no single color to build from — never silently guessed)."""

    def norm(v: str, label: str, required: bool = False) -> str:
        v = (v or "").strip()
        if not v:
            if required:
                raise ValueError(f"{label} color is required")
            return ""
        if re.fullmatch(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}", v):
            v = "#" + v
        p = parse_color(v)
        if not p:
            raise ValueError(f"{label}: {v!r} is not a CSS color")
        if p[1] < 0.999:
            raise ValueError(f"{label}: {v!r} is translucent (alpha {p[1]:.2f}) — give an opaque brand color")
        return cc.to_hex(p[0])

    return {
        "primary": norm(primary, "primary", True),
        "secondary": norm(secondary, "secondary"),
        "neutral": norm(neutral, "neutral"),
    }


_FONT_OK_RE = re.compile(r"""^[\w\s,"'.-]{1,200}$""")


def clean_font_family(stack: str) -> str:
    """A font stack safe to write into a CSS declaration: names, spaces, commas, quotes, dots
    and hyphens only. It usually comes from a HOSTILE site's CSS, so anything that could close
    the declaration or the rule (``;``, ``}``, ``<``, ``/``, ``(``…) is rejected, not escaped."""
    ff = " ".join((stack or "").split()).strip().rstrip(";").strip()
    if not ff:
        return ""
    if not _FONT_OK_RE.match(ff) or ff.count('"') % 2 or ff.count("'") % 2:
        raise ValueError(f"font_family {stack[:60]!r} is not a plain font stack (names, commas, quotes only)")
    return ff


# ══════════════════════════════════════════════════════════════════════════════
# Role inference over the LIVE contract
# ══════════════════════════════════════════════════════════════════════════════

# (pattern over the var name after `--pl-color-`, role). First match wins; order matters —
# `fg-on-accent` must be read before the generic `fg*`, `accent-fg` before `accent-*`.
ROLE_TABLE: tuple[tuple[str, str], ...] = (
    (r"^brand(-|$)", "brand-mark"),
    (r"^(bg|background|surface|canvas)$", "ground"),
    (r"^(bg|background|surface)-", "surface"),
    (r"^(overlay|scrim|backdrop)", "scrim"),
    (r"^(fg|text|foreground)-on-", "on-accent"),
    (r"^on-(accent|primary|brand)$", "on-accent"),
    (r"^(fg|text|foreground)$", "text"),
    (r"^(fg|text|foreground)-(muted|secondary)$", "text-muted"),
    (r"^(fg|text|foreground)-(subtle|tertiary|placeholder|disabled|faint)$", "text-subtle"),
    (r"^(fg|text|foreground)-", "text-muted"),
    (r"^(border|divider|separator|outline|line)", "line"),
    (r"^(accent|primary|link)-(fg|text)$|^link$", "accent-text"),
    (r"^(accent|primary)$", "accent"),
    (r"^(accent|primary)-", "accent-variant"),
    (r"^(focus|ring)", "focus"),
    (r"^(status-)?(success|positive|ok)$", "status"),
    (r"^(status-)?(warning|caution|warn)$", "status"),
    (r"^(status-)?(error|danger|negative|critical)$", "status"),
    (r"^(status-)?(info|notice|neutral)$", "status"),
    (r"^chart-(series)?-?\d+$", "chart-series"),
    (r"^chart-(grid|gridline|line)", "line"),
    (r"^chart-(axis|label|tick|text)", "text-muted"),
)

ROLE_DOC = {
    "brand-mark": "literal brand marks — theme-invariant; mapped by the DS's own hue/lightness relation to its default accent",
    "ground": "page ground — the DS's default lightness, tinted toward the brand hue",
    "surface": "surfaces — keep the DS's elevation relation (L offset from the ground), brand-tinted",
    "scrim": "overlay scrim — alpha kept, tinted",
    "on-accent": "text on an accent fill — whichever extreme clears AA",
    "text": "primary text — DS lightness, faint brand tint",
    "text-muted": "secondary text — DS lightness, faint brand tint",
    "text-subtle": "tertiary/placeholder text — DS lightness (3:1 tier)",
    "line": "borders/dividers — alpha kept, tinted (decorative: not contrast-gated)",
    "accent": "interactive accent — the brand, stepped (theme.scale) until it clears 3:1 on the ground",
    "accent-variant": "accent states — the DS's lightness offset from its accent",
    "accent-text": "accent as text (links) — DS offset from accent, repaired to 4.5:1",
    "focus": "focus ring — DS offset from accent, 3:1",
    "status": "status — the DS's hue nudged ≤10° toward the brand, kept recognizable",
    "chart-series": "chart series — the DS's set rotated so series 1 lands on the brand hue",
    "hue-shift": "fallback — the DS's value hue-rotated by (brand hue − default accent hue)",
    "passthrough": "not a literal color (var()/gradient) — left to the DS default",
}


def infer_role(var: str, default_value: str = "") -> str:
    name = var.removeprefix("--pl-color-")
    for pat, role in ROLE_TABLE:
        if re.search(pat, name):
            return role
    p = parse_color(default_value)
    if p is None:
        return "passthrough"
    return "line" if p[1] < 0.999 else "hue-shift"


def color_vars(contract: dict) -> list[str]:
    """Every ``--pl-color-*`` var the contract defines, in contract order (dark set first)."""
    seen = dict.fromkeys(k for k in contract.get("dark", {}) if k.startswith("--pl-color-"))
    seen.update(dict.fromkeys(k for k in contract.get("light", {}) if k.startswith("--pl-color-")))
    return list(seen)


# ══════════════════════════════════════════════════════════════════════════════
# Generation
# ══════════════════════════════════════════════════════════════════════════════

# Minimum contrast per role pairing (WCAG 2.x): 4.5 body text, 3.0 large text / UI graphics.
NEED = {"text": 4.5, "text-muted": 4.5, "accent-text": 4.5, "on-accent": 4.5, "text-subtle": 3.0,
        "accent": 3.0, "focus": 3.0, "status": 3.0, "chart-series": 3.0}


def _parse_default(value: str):
    p = parse_color(value)
    if not p:
        return None
    L, C, h = cc.rgb_to_lch(p[0])
    return {"L": L, "C": C, "h": h, "alpha": p[1], "rgb": p[0]}


def _first(roles: dict[str, str], role: str, prefer: str = "") -> str | None:
    cands = [v for v, r in roles.items() if r == role]
    if prefer:
        for v in cands:
            if v.endswith(prefer):
                return v
    return cands[0] if cands else None


def _pick_accent(brand_hex: str, grounds: list[str], light_on: str, dark_on: str, theme: str, notes: list, band: tuple[float, float] = (0.0, 100.0), prefer_on: str = "") -> str:
    """The brand if it clears 3:1 on the grounds AND takes an AA on-accent AND sits in the DS's
    lightness band for this theme (dark: not much darker than the DS's own dark accent; light:
    not much lighter than its light accent — "lavender leads on dark, the deep step on light").
    Else the nearest ``theme.scale(brand)`` step that does; else an LCH lightness search.
    ``prefer_on`` is the DS's own on-accent polarity for this theme (dark text on the dark
    theme's accent, white on the light theme's): steps that take it are preferred, so a pastel
    brand deepens to a real fill on light instead of settling at a muddy mid-tone."""

    def ok(hx: str, on_pref: bool = False) -> bool:
        if not all(ratio(hx, g) >= 3.0 for g in grounds):
            return False
        if on_pref and prefer_on:
            return ratio(hx, prefer_on) >= 4.5
        return max(ratio(hx, light_on), ratio(hx, dark_on)) >= 4.5

    def in_band(hx: str) -> bool:
        return band[0] <= _lch(hx)[0] <= band[1]

    bL = _lch(brand_hex)[0]
    if ok(brand_hex) and in_band(brand_hex):
        return brand_hex
    steps = _theme.scale(brand_hex)
    ranked = sorted(zip(_theme.SCALE_STOPS, steps), key=lambda s: abs(_lch(s[1])[0] - bL))
    why = "fails 3:1 on the ground / AA on-accent" if not ok(brand_hex) else "sits outside the DS's accent lightness band"
    for want_band, on_pref in ((True, True), (True, False), (False, True), (False, False)):
        if not want_band and ok(brand_hex):
            return brand_hex  # no in-band step works: the brand itself beats an off-band step
        for stop, hx in ranked:
            if ok(hx, on_pref) and (in_band(hx) or not want_band):
                notes.append({"theme": theme, "var": "--pl-color-accent", "from": brand_hex, "to": hx,
                              "reason": f"brand {why} on {theme} → theme.scale step {stop}"})
                return hx
    L, C, h = _lch(brand_hex)
    for d in range(1, 101):
        for L2 in (L + d, L - d):
            if 0 <= L2 <= 100:
                hx = _from_lch(L2, C, h)
                if ok(hx):
                    notes.append({"theme": theme, "var": "--pl-color-accent", "from": brand_hex, "to": hx, "reason": "brand re-lit in LCH to clear 3:1 + AA on-accent"})
                    return hx
    return brand_hex


def generate(seeds: dict, contract: dict, *, name: str = "brand", font_family: str = "", radius: str = "", strict: bool = False, strict_status: bool = False) -> dict:
    """A full dark + light theme for EVERY ``--pl-color-*`` var in ``contract``
    (``tokens.parse_css`` output). Returns::

        {name, seeds, dark:{var: css}, light:{var: css}, base:{var: css}, roles:{var: role},
         adjustments:[{theme, var, from, to, reason}], repairs:[…same…],
         contrast:[{theme, fg, bg, fg_value, bg_value, ratio, need, status}], passthrough:[vars]}

    ``status`` is ``pass`` | ``repaired`` | ``fail`` (fail only if lightness ran out — reported,
    never hidden). ``strict`` holds status colors AND the text-subtle tier to 4.5:1: the kit's
    .pl-badge uses status as 11px text and text-subtle is placeholder text, which WCAG treats as
    text. The default (3:1 for both) follows the DS's own intent — status as hue marks,
    text-subtle as the tertiary/disabled tier — and says so in ``notes``. ``strict_status`` is the
    older name for ``strict``.
    """
    strict = strict or strict_status
    primary = seeds.get("primary") or ""
    if not _hex(primary):
        raise ValueError(f"primary seed {primary!r} is not a color")
    primary = _hex(primary)
    font_family = clean_font_family(font_family)
    secondary = _hex(seeds.get("secondary") or "") or ""
    neutral = _hex(seeds.get("neutral") or "") or ""
    vars_ = color_vars(contract)
    if not vars_:
        raise ValueError("the contract defines no --pl-color-* vars — is this the DS's tokens.css?")
    dark_def, light_def = contract.get("dark", {}), contract.get("light", {})
    roles = {v: infer_role(v, dark_def.get(v, light_def.get(v, ""))) for v in vars_}

    _, bC, bh = _lch(primary)
    chromatic = bC >= 8
    # Neutral tint: the neutral seed's hue if it has one, else the brand's; always low chroma.
    if neutral and _lch(neutral)[1] >= 1.5:
        nh, nC = _lch(neutral)[2], min(_lch(neutral)[1], 6.0)
    elif neutral:
        nh, nC = bh, 0.0
    else:
        nh, nC = bh, min(bC * 0.045, 2.8) if chromatic else 0.0

    # The DS's own default accent (dark set) anchors every hue-relation.
    acc_var = _first(roles, "accent")
    def_acc = _parse_default(dark_def.get(acc_var, "")) if acc_var else None
    hue_rot = _hue_delta(def_acc["h"], bh) if (def_acc and chromatic) else 0.0

    out: dict = {"name": name, "seeds": {"primary": primary, "secondary": secondary, "neutral": neutral, "mode_hint": seeds.get("mode_hint", "")},
                 "dark": {}, "light": {}, "base": {}, "roles": roles, "adjustments": [], "repairs": [], "contrast": [], "passthrough": [],
                 "defaults": non_color_defaults(contract), "notes": []}
    if not strict:
        out["notes"].append(
            "text-subtle (placeholder/tertiary) and status colors are held to 3:1, the DS's intent; "
            "WCAG treats placeholder and badge text as text (4.5:1) — pass strict=True to enforce that"
        )

    # ── brand marks: theme-invariant, one value in both themes ──
    marks = [v for v in vars_ if roles[v] == "brand-mark"]
    mark_def = {v: _parse_default(dark_def.get(v, "")) for v in marks}
    # Group marks into families by NAME stem (`brand-<family>[-<step>]`): a DS may carry two
    # brand families whose hues sit within a few degrees (lavender/indigo), so hue can't split them.
    families: dict[str, list[str]] = {}
    for v in marks:
        if mark_def[v]:
            stem = v.removeprefix("--pl-color-brand-").split("-", 1)[0] or "brand"
            families.setdefault(stem, []).append(v)
    for fi, fam in enumerate(families.values()):
        use_secondary = fi > 0 and bool(secondary)
        if use_secondary:
            # The second family follows the secondary seed, anchored on its own base step.
            base = next((v for v in fam if v.count("-") == min(x.count("-") for x in fam)), fam[0])
            anchor_def, seed = mark_def[base], secondary
        else:
            anchor_def, seed = (def_acc or mark_def[fam[0]]), primary
        sL, sC, sh = _lch(seed)
        for v in fam:
            d = mark_def[v]
            cscale = d["C"] / anchor_def["C"] if anchor_def["C"] > 1 else 1.0
            dL = d["L"] - anchor_def["L"]
            L2 = sL + dL if 4 <= sL + dL <= 96 else sL - dL  # ran off the end: step the other way
            hx = _from_lch(L2, sC * cscale, sh + _hue_delta(anchor_def["h"], d["h"]))
            out["dark"][v] = out["light"][v] = hx

    for theme, defaults in (("dark", dark_def), ("light", light_def)):
        t = out[theme]
        D = {v: _parse_default(defaults.get(v, dark_def.get(v, ""))) for v in vars_}
        ground = _first(roles, "ground")
        gd = D.get(ground) if ground else None
        is_dark = (gd["L"] < 50) if gd else theme == "dark"

        # Surfaces + ground: DS lightness, brand-tinted neutral hue.
        for v in vars_:
            if roles[v] in ("ground", "surface") and D[v]:
                d = D[v]
                if d["alpha"] < 0.999:
                    t[v] = to_css(cc.fit_lch((d["L"], max(d["C"], nC), nh)), d["alpha"])
                else:
                    t[v] = _from_lch(d["L"], nC * (1.0 if is_dark else 0.8), nh)
        grounds = [v for v in vars_ if roles[v] in ("ground", "surface") and v in t and parse_color(t[v])[1] >= 0.999]
        ground_vals = [t[v] for v in grounds] or ["#000000" if is_dark else "#ffffff"]

        # Text tiers: DS lightness, faint tint.
        for v in vars_:
            if roles[v] in ("text", "text-muted", "text-subtle") and D[v] and D[v]["alpha"] >= 0.999:
                t[v] = _from_lch(D[v]["L"], nC * (0.5 if roles[v] == "text" else 0.9), nh)

        # Accent: brand, stepped per theme.
        light_on = _from_lch(99.5, min(nC, 1.5), nh)
        dark_on = t.get(ground) if (ground and is_dark) else _from_lch(8, min(nC, 3), nh)
        dark_on = dark_on if _lch(dark_on)[0] < 20 else _from_lch(8, min(nC, 3), nh)
        dA = D.get(acc_var) if acc_var else None
        band = ((dA["L"] - 14, 100.0) if is_dark else (0.0, dA["L"] + 14)) if dA else (0.0, 100.0)
        on_var = _first(roles, "on-accent")
        prefer_on = (dark_on if D[on_var]["L"] < 50 else light_on) if (on_var and D.get(on_var)) else ""
        acc = _pick_accent(primary, [t[g] for g in grounds if g == ground or "raised" in g] or ground_vals, light_on, dark_on, theme, out["adjustments"], band, prefer_on)
        aL, aC, ah = _lch(acc)
        if acc_var:
            t[acc_var] = acc

        def rel(v: str, base_L=aL, base_C=aC, base_h=ah, D=D, dA=dA, acc=acc) -> str:
            d = D[v]
            if not dA or not d:
                return acc
            dL = d["L"] - dA["L"]
            L2 = base_L + dL
            if dL and not (2 <= L2 <= 98):  # ran off the end: step the other way instead
                L2 = base_L - dL
            cs = d["C"] / dA["C"] if dA["C"] > 1 else 1.0
            return _from_lch(L2, base_C * min(cs, 1.4), base_h + _hue_delta(dA["h"], d["h"]))

        for v in vars_:
            r = roles[v]
            if r in ("accent-variant", "accent-text", "focus") and D[v]:
                t[v] = rel(v)
                if D[v]["alpha"] < 0.999:
                    t[v] = to_css(parse_color(t[v])[0], D[v]["alpha"])

        # On-accent: whichever extreme reads best on the accent and all its variants.
        fills = [t[v] for v in vars_ if roles[v] in ("accent", "accent-variant") and v in t and parse_color(t[v])[1] >= 0.999]
        for v in vars_:
            if roles[v] == "on-accent":
                t[v] = max((light_on, dark_on), key=lambda c: (min(ratio(c, f) for f in fills) if fills else 0))

        # Status: DS hue nudged ≤10° toward the brand — recognizable, but in the family.
        for v in vars_:
            if roles[v] == "status" and D[v]:
                d = D[v]
                nudge = max(-10.0, min(10.0, _hue_delta(d["h"], bh) * 0.12)) if chromatic else 0.0
                t[v] = _from_lch(d["L"], d["C"], d["h"] + nudge)

        # Chart series: rotate the DS's set so series 1 lands on the brand hue (keeps spacing).
        series = [v for v in vars_ if roles[v] == "chart-series"]
        if series and D[series[0]]:
            rot = _hue_delta(D[series[0]]["h"], bh) if chromatic else 0.0
            for v in series:
                if D[v]:
                    t[v] = _from_lch(D[v]["L"], D[v]["C"], D[v]["h"] + rot)
            _spread_series(out, theme, series, D)

        # Lines + scrims: alpha kept, hue tinted.
        for v in vars_:
            if roles[v] in ("line", "scrim") and D[v]:
                d = D[v]
                C2 = min(max(d["C"], nC * 1.5), 10.0) if d["alpha"] < 0.999 or d["C"] < 12 else d["C"]
                t[v] = to_css(cc.fit_lch((d["L"], C2, nh)), d["alpha"])

        # Fallbacks: anything the table didn't claim keeps its hue relation to the brand.
        for v in vars_:
            if v in t:
                continue
            d = D[v]
            if not d:
                if v not in out["passthrough"]:
                    out["passthrough"].append(v)
                continue
            t[v] = to_css(cc.fit_lch((d["L"], d["C"], d["h"] + hue_rot)), d["alpha"])

        _repair(out, theme, grounds, strict)

    # Non-color extras (base block only — invariant across themes).
    base_all = {**dark_def}
    if font_family:
        fv = next((k for k in base_all if k.startswith("--pl-font-") and "sans" in k), None) or next((k for k in base_all if k.startswith("--pl-font-") and "family" in k), None)
        if fv:
            ff = font_family.strip().rstrip(";")
            fallback = base_all.get(fv, "")
            tail = fallback.split(",", 1)[1].strip() if "," in fallback else "system-ui, sans-serif"
            if not _FONT_OK_RE.match(tail):
                tail = "system-ui, sans-serif"
            out["base"][fv] = ff if "," in ff else f"{ff}, {tail}"
    if radius:
        rv = "--pl-radius" if "--pl-radius" in base_all else next((k for k in base_all if k.startswith("--pl-radius")), None)
        m = re.fullmatch(r"\s*(" + _NUM + r")\s*(px|rem)?\s*", radius)
        if rv and m:
            out["base"][rv] = f"{m.group(1)}{m.group(2) or 'px'}"
    return out


def _min_de(hexes: list[str]) -> float:
    return min((_delta_e(a, b) for i, a in enumerate(hexes) for b in hexes[i + 1 :]), default=999.0)


def _spread_series(out: dict, theme: str, series: list[str], D: dict) -> None:
    """Keep rotated chart series distinguishable. Rotation preserves the DS's hue spacing, but
    gamut-fitting at the new hues can collapse two series toward each other (e.g. two ambers).
    Floor: 80% of the DS's OWN minimum pairwise ΔE (never below 12); a series closer than that
    to an earlier one is rotated further, ±8° at a time up to ±64°, keeping its L and C."""
    t = out[theme]
    defaults = [cc.to_hex(D[v]["rgb"]) for v in series if D.get(v)]
    floor = max(12.0, 0.8 * _min_de(defaults)) if len(defaults) > 1 else 12.0
    placed: list[str] = []
    for v in series:
        if v not in t or not D.get(v):
            continue
        cur = t[v]
        if placed and min(_delta_e(cur, p) for p in placed) < floor:
            L, C, h = _lch(cur)
            best, best_d = cur, min(_delta_e(cur, p) for p in placed)
            for k in range(1, 9):
                for sign in (1, -1):
                    cand = _from_lch(L, C, h + sign * 8 * k)
                    d = min(_delta_e(cand, p) for p in placed)
                    if d > best_d:
                        best, best_d = cand, d
                if best_d >= floor:
                    break
            if best != cur:
                out["adjustments"].append({"theme": theme, "var": v, "from": cur, "to": best,
                                           "reason": f"chart series within ΔE {min(_delta_e(cur, p) for p in placed):.1f} of another (< {floor:.1f}) → hue rotated"})
                t[v] = cur = best
        placed.append(cur)


def _pairs(roles: dict[str, str], grounds: list[str], strict: bool) -> list[tuple[str, str, float, str]]:
    """(fg var, bg var, need, adjust) — adjust ∈ fg|either (on-accent may move the fill)."""
    pairs = []
    fills = [v for v, r in roles.items() if r in ("accent", "accent-variant")]
    for v, r in roles.items():
        if r in ("text", "text-muted", "accent-text", "text-subtle", "accent", "focus", "status", "chart-series"):
            need = 4.5 if (strict and r in ("status", "text-subtle")) else NEED[r]
            for g in grounds:
                pairs.append((v, g, need, "fg"))
        elif r == "on-accent":
            for f in fills:
                pairs.append((v, f, 4.5, "either"))
    return pairs


def _nudge(value: str, away_from: str, need: float, other: str) -> tuple[str, bool]:
    """Move ``value``'s LCH lightness away from ``away_from`` until ``ratio(value, other) ≥ need``."""
    p = parse_color(value)
    L, C, h = cc.rgb_to_lch(p[0])
    step = 0.5 if _lch(cc.to_hex(parse_color(away_from)[0]))[0] < L else -0.5
    cur = value
    for _ in range(220):
        if ratio(cur, other) >= need:
            return cur, True
        L += step
        if not 0 <= L <= 100:
            break
        cur = to_css(cc.fit_lch((L, C, h)), p[1])
    return cur, ratio(cur, other) >= need


def _repair(out: dict, theme: str, grounds: list[str], strict: bool) -> None:
    t, roles = out[theme], out["roles"]
    pairs = [p for p in _pairs(roles, grounds, strict) if p[0] in t and p[1] in t]
    original = dict(t)
    reasons: dict[str, str] = {}
    for _ in range(6):
        changed = False
        for fg, bg, need, adjust in pairs:
            r0 = ratio(t[fg], t[bg])
            if r0 >= need:
                continue
            short_bg, short_fg = bg.removeprefix("--pl-color-"), fg.removeprefix("--pl-color-")
            new, ok = _nudge(t[fg], t[bg], need, t[bg])
            reasons.setdefault(fg, f"{r0:.2f}:1 on {short_bg} < {need:g} → L nudged")
            t[fg] = new
            changed = True
            if not ok and adjust == "either":
                t[bg], _ = _nudge(t[bg], t[fg], need, t[fg])
                reasons.setdefault(bg, f"{short_fg} ran out of lightness on it → the fill moved to clear {need:g}")
        if not changed:
            break
    for v in t:
        if t[v] != original.get(v):
            out["repairs"].append({"theme": theme, "var": v, "from": original[v], "to": t[v], "reason": reasons.get(v, "repaired")})
    for fg, bg, need, _ in pairs:
        r = ratio(t[fg], t[bg])
        was = ratio(original[fg], original[bg])
        out["contrast"].append({
            "theme": theme, "fg": fg, "bg": bg, "fg_value": t[fg], "bg_value": t[bg], "ratio": round(r, 2), "need": need,
            "status": "fail" if r < need else ("repaired" if was < need else "pass"),
        })


# ══════════════════════════════════════════════════════════════════════════════
# Rendering
# ══════════════════════════════════════════════════════════════════════════════

_SCOPE_RE = re.compile(r"^[\w\s\-\[\]=\"':.#>+~,*()]+$")
_ROOT_HEAD_RE = re.compile(r"^(?::root|html)(?=$|[.#\[:\s>+~])", re.IGNORECASE)


def _split_top_level(sel: str) -> list[str]:
    """Split a selector list on top-level commas (not inside [] () or quotes)."""
    parts, depth, quote, cur = [], 0, "", []
    for ch in sel:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur).strip())
    return parts


def scope_parts(scope: str) -> list[str]:
    """Validate + normalize a scope into its selector parts. ``html`` becomes ``:root`` — the DS
    writes ``:root`` blocks, and a bare ``html`` (specificity 0,0,1) would LOSE to them. Raises
    ``ValueError`` on anything that isn't a plain selector list."""
    s = " ".join((scope or ":root").split())
    if not _SCOPE_RE.match(s) or any(c in s for c in "{};<") or "/*" in s:
        raise ValueError(f"scope {scope!r} is not a plain CSS selector")
    parts = _split_top_level(s)
    if any(not p for p in parts):
        raise ValueError(f"scope {scope!r} has an empty selector in its list")
    return [_ROOT_HEAD_RE.sub(":root", p) for p in parts]


def normalize_scope(scope: str) -> str:
    """The scope exactly as ``render_css`` writes it (pass this to ``tokens.parse_css``)."""
    return ", ".join(scope_parts(scope))


def _forced_part(part: str, mode: str) -> str:
    attr = f'[data-theme="{mode}"]'
    if part.startswith(":root"):
        # The root element itself carries data-theme: qualify the root compound in place.
        m = re.match(r"^(:root[^\s>+~]*)(.*)$", part)
        return f"{m.group(1)}{attr}{m.group(2)}"
    return f":root{attr} {part}, {part}{attr}"


def _forced(scope: str, mode: str) -> str:
    return ", ".join(_forced_part(p, mode) for p in scope_parts(scope))


def render_css(theme: dict, scope: str = ":root") -> str:
    """The theme in the DS's exact 4-block shape: base (dark default) → ``@media
    (prefers-color-scheme: light)`` → forced ``[data-theme="light"]`` → forced
    ``[data-theme="dark"]``. Themed blocks carry only the vars that differ between the two themes,
    as the DS's own build does. ``scope`` other than ``:root`` scopes it for white-label
    (``[data-brand="acme"]``; comma lists are qualified part by part) — the forced blocks then
    match the app's ``:root[data-theme]`` or the scoped element's own ``data-theme``.
    ``tokens.parse_css(css, scope=normalize_scope(scope))`` reads a scoped render back."""
    scope = normalize_scope(scope)
    dark, light, base = theme["dark"], theme["light"], theme.get("base", {})
    themed = [v for v in dark if light.get(v) != dark[v]] + [v for v in light if v not in dark]
    s = theme.get("seeds", {})
    head = (
        f"/* {re.sub(r'[^A-Za-z0-9 _-]', '', str(theme.get('name', 'brand')))[:48]} — generated by design-system-plugin themegen from "
        f"primary {s.get('primary')}" + (f", secondary {s.get('secondary')}" if s.get("secondary") else "")
        + (f", neutral {s.get('neutral')}" if s.get("neutral") else "")
        + ". Override layer over the DS tokens.css: load it AFTER tokens.css. */\n"
    )

    def block(sel: str, scheme: str, items: list[tuple[str, str]], indent: str = "") -> str:
        body = "".join(f"{indent}  {k}: {v};\n" for k, v in items)
        return f"{indent}{sel} {{\n{indent}  color-scheme: {scheme};\n{body}{indent}}}\n"

    parts = [head, block(scope, "dark", list(dark.items()) + list(base.items()))]
    light_items = [(v, light.get(v, dark.get(v))) for v in themed]
    dark_items = [(v, dark.get(v, light.get(v))) for v in themed]
    parts.append("\n/* Light — the OS preference drives it; the explicit [data-theme] force below beats it. */\n")
    parts.append("@media (prefers-color-scheme: light) {\n" + block(scope, "light", light_items, "  ") + "}\n")
    parts.append("\n/* Explicit theme force — wins over the OS preference above. */\n")
    parts.append(block(_forced(scope, "light"), "light", light_items))
    parts.append(block(_forced(scope, "dark"), "dark", dark_items))
    return "".join(parts)


def apply_maps(theme: dict) -> dict:
    """``{"dark": {...}, "light": {...}}`` — each theme's colors plus the base font/radius
    overrides, exactly the ``overrides_json`` ``theme_apply`` takes for that mode."""
    base = theme.get("base", {})
    return {m: {**theme[m], **base} for m in ("dark", "light")}


def render_json(theme: dict) -> str:
    """The theme as JSON. ``dark``/``light`` are the color maps, ``base`` the font/radius
    overrides, and ``apply`` the two merged per mode — ready-made ``theme_apply`` maps."""
    keep = ("name", "seeds", "dark", "light", "base", "roles", "adjustments", "repairs", "contrast", "passthrough", "notes")
    data = {k: theme.get(k) for k in keep}
    data["apply"] = apply_maps(theme)
    return json.dumps(data, indent=1)


def contrast_report(theme: dict, *, only_notable: bool = False) -> str:
    """A text table of every checked pair → ratio → pass/repaired/fail, per theme."""
    rows = theme.get("contrast", [])
    lines = []
    for th in ("dark", "light"):
        rs = [r for r in rows if r["theme"] == th]
        if not rs:
            continue
        n_rep = sum(r["status"] == "repaired" for r in rs)
        n_fail = sum(r["status"] == "fail" for r in rs)
        lines.append(f"{th}: {len(rs)} pairs — {len(rs) - n_rep - n_fail} pass, {n_rep} repaired, {n_fail} FAIL")
        for r in rs:
            if only_notable and r["status"] == "pass":
                continue
            fg, bg = r["fg"].removeprefix("--pl-color-"), r["bg"].removeprefix("--pl-color-")
            lines.append(f"  {fg:<18} on {bg:<12} {r['ratio']:>5.2f}:1  (≥{r['need']})  {r['status']}")
    return "\n".join(lines)


def summary_tokens(theme: dict) -> list[tuple[str, str, str]]:
    """(short name, dark, light) for the vars an operator judges a theme by."""
    want = ("bg", "bg-raised", "bg-subtle", "fg", "fg-muted", "accent", "accent-hover", "accent-fg", "fg-on-accent", "focus",
            "status-success", "status-warning", "status-error", "status-info")
    out = []
    for w in want:
        v = f"--pl-color-{w}"
        if v in theme["dark"]:
            out.append((w, theme["dark"][v], theme["light"].get(v, "")))
    return out


# ── preview ─────────────────────────────────────────────────────────────────


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_PREVIEW_CSS = r"""
*{box-sizing:border-box}
body{margin:0;background:#101012;font-family:var(--pl-font-sans,system-ui,-apple-system,sans-serif);-webkit-font-smoothing:antialiased}
.tg-top{padding:20px 24px 4px;color:#e6e6e8;display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 18px}
.tg-top h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.01em}
.tg-top .tg-meta{font:12px/1.4 var(--pl-font-mono,ui-monospace,monospace);color:#9a9aa3;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.tg-seed{display:inline-flex;align-items:center;gap:6px}
.tg-seed i{width:12px;height:12px;border-radius:3px;display:inline-block;box-shadow:inset 0 0 0 1px rgba(255,255,255,.2)}
.tg-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding:16px 24px 24px}
@media (max-width:980px){.tg-grid{grid-template-columns:1fr}}
.tg-panel{background:var(--pl-color-bg);color:var(--pl-color-fg);border-radius:10px;padding:22px;border:1px solid var(--pl-color-border);
  font-size:var(--pl-font-base-size,14px);line-height:var(--pl-font-line-height-body,1.6);color-scheme:inherit;min-width:0}
.tg-mode{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.tg-mode b{font:600 11px/1 var(--pl-font-mono,ui-monospace,monospace);letter-spacing:.08em;text-transform:uppercase;color:var(--pl-color-fg-muted)}
.tg-h{font-size:22px;line-height:var(--pl-font-line-height-heading,1.2);font-weight:600;margin:0 0 6px;letter-spacing:-.01em}
.tg-p{margin:0 0 4px;color:var(--pl-color-fg-muted)}
.tg-cap{font-size:12px;color:var(--pl-color-fg-subtle)}
.tg-lbl{font:600 10.5px/1 var(--pl-font-mono,ui-monospace,monospace);letter-spacing:.08em;text-transform:uppercase;color:var(--pl-color-fg-subtle);margin:20px 0 8px}
.tg-surfaces{display:grid;grid-template-columns:repeat(5,1fr);gap:6px}
.tg-surf{height:52px;border-radius:var(--pl-radius,4px);border:1px solid var(--pl-color-border);padding:6px 7px;font:10.5px/1.2 var(--pl-font-mono,ui-monospace,monospace);color:var(--pl-color-fg-muted);display:flex;flex-direction:column;justify-content:flex-end;overflow:hidden}
.tg-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.tg-btn{display:inline-flex;align-items:center;gap:6px;padding:.5rem .95rem;font:500 13px/1.2 var(--pl-font-sans,system-ui,sans-serif);border-radius:var(--pl-radius,4px);cursor:pointer;border:var(--pl-border-width,1px) solid transparent}
.tg-btn--accent{background:var(--pl-color-accent);color:var(--pl-color-fg-on-accent)}
.tg-btn--accent:hover,.tg-btn--accent.is-hover{background:var(--pl-color-accent-hover)}
:where(.pl-btn){display:inline-flex;align-items:center;padding:.5rem .9rem;font:400 13px/1.2 var(--pl-font-sans,system-ui,sans-serif);color:var(--pl-color-fg);background:transparent;border:var(--pl-border-width,1px) solid var(--pl-color-border-strong);border-radius:var(--pl-radius,4px)}
:where(.pl-btn--primary){border-color:var(--pl-color-fg)}
:where(.pl-btn--ghost){border-color:transparent}
.tg-link{color:var(--pl-color-accent-fg);text-decoration:underline;text-underline-offset:3px;text-decoration-color:color-mix(in srgb,var(--pl-color-accent-fg) 45%,transparent)}
:where(.pl-alert){display:flex;gap:10px;padding:10px 12px;background:var(--pl-color-bg-raised);border:var(--pl-border-width,1px) solid var(--pl-color-border);border-left-width:3px;border-radius:var(--pl-radius,4px);color:var(--pl-color-fg);font-size:13px;line-height:1.45}
:where(.pl-alert--success){border-left-color:var(--pl-color-status-success)}
:where(.pl-alert--warning){border-left-color:var(--pl-color-status-warning)}
:where(.pl-alert--error){border-left-color:var(--pl-color-status-error)}
:where(.pl-alert--info){border-left-color:var(--pl-color-status-info)}
:where(.pl-alert__text){color:var(--pl-color-fg-muted)}
.tg-alerts{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.tg-dotc{width:8px;height:8px;border-radius:50%;margin-top:6px;flex:none}
:where(.pl-badge){display:inline-flex;align-items:center;gap:.35rem;padding:.15rem .5rem;font:11px/1.4 var(--pl-font-mono,ui-monospace,monospace);text-transform:lowercase;letter-spacing:.02em;color:var(--pl-color-fg-muted);background:var(--pl-color-bg-raised);border:var(--pl-border-width,1px) solid var(--pl-color-border);border-radius:var(--pl-radius,4px)}
:where(.pl-badge--success){color:var(--pl-color-status-success);border-color:color-mix(in oklch,var(--pl-color-status-success) 35%,transparent)}
:where(.pl-badge--warning){color:var(--pl-color-status-warning);border-color:color-mix(in oklch,var(--pl-color-status-warning) 35%,transparent)}
:where(.pl-badge--error){color:var(--pl-color-status-error);border-color:color-mix(in oklch,var(--pl-color-status-error) 35%,transparent)}
:where(.pl-badge--info){color:var(--pl-color-status-info);border-color:color-mix(in oklch,var(--pl-color-status-info) 35%,transparent)}
.tg-badge--accent{color:var(--pl-color-accent-fg);border-color:color-mix(in oklch,var(--pl-color-accent) 40%,transparent)}
.tg-two{display:grid;grid-template-columns:1.1fr 1fr;gap:10px}
:where(.pl-card){background:var(--pl-color-bg-raised);border:var(--pl-border-width,1px) solid var(--pl-color-border);border-radius:var(--pl-radius,4px);padding:var(--pl-space-4,16px)}
.tg-card{box-shadow:var(--pl-shadow-card,none)}
.tg-stat{font-size:26px;font-weight:600;letter-spacing:-.02em;line-height:1.1;margin:4px 0 2px}
.tg-delta{font-size:12px;color:var(--pl-color-status-success)}
.tg-bar{height:6px;border-radius:99px;background:var(--pl-color-bg-subtle);margin-top:12px;overflow:hidden}
.tg-bar>i{display:block;height:100%;width:64%;background:var(--pl-color-accent);border-radius:99px}
:where(.pl-field__label){display:block;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--pl-color-fg-muted);margin-bottom:6px}
:where(.pl-input){width:100%;background:var(--pl-color-bg-raised);border:var(--pl-border-width,1px) solid var(--pl-color-border);color:var(--pl-color-fg);border-radius:var(--pl-radius,4px);padding:8px 10px;font:13px var(--pl-font-sans,system-ui,sans-serif)}
.pl-input::placeholder{color:var(--pl-color-fg-subtle)}
.tg-focus{outline:2px solid var(--pl-color-focus);outline-offset:2px;border-color:var(--pl-color-border-strong)}
.tg-help{font-size:12px;color:var(--pl-color-fg-subtle);margin-top:6px}
.tg-chart{position:relative;height:120px;display:flex;align-items:flex-end;gap:10px;padding:0 4px 18px 26px;
  background:repeating-linear-gradient(to top,var(--pl-color-chart-grid,var(--pl-color-border)) 0 1px,transparent 1px 25px);border-bottom:1px solid var(--pl-color-chart-grid,var(--pl-color-border))}
.tg-chart>.tg-b{flex:1;border-radius:3px 3px 0 0;position:relative;min-width:0}
.tg-chart>.tg-b span{position:absolute;bottom:-17px;left:50%;transform:translateX(-50%);font:10px var(--pl-font-mono,ui-monospace,monospace);color:var(--pl-color-chart-axis,var(--pl-color-fg-muted))}
.tg-yax{position:absolute;left:0;top:0;bottom:18px;display:flex;flex-direction:column;justify-content:space-between;font:10px var(--pl-font-mono,ui-monospace,monospace);color:var(--pl-color-chart-axis,var(--pl-color-fg-muted))}
.tg-marks{display:flex;gap:0;border-radius:var(--pl-radius,4px);overflow:hidden;height:22px}
.tg-marks>i{flex:1}
.tg-report{margin:0 24px 28px;background:#16161a;border:1px solid #26262c;border-radius:10px;color:#d8d8dc;padding:16px 18px;font-size:12.5px}
.tg-report h2{font-size:13px;margin:0 0 10px;font-weight:600}
.tg-report table{width:100%;border-collapse:collapse;font:11.5px/1.5 var(--pl-font-mono,ui-monospace,monospace)}
.tg-report td,.tg-report th{padding:3px 8px;border-bottom:1px solid #222228;text-align:left;white-space:nowrap}
.tg-report th{color:#8e8e98;font-weight:500}
.tg-report small{color:#7a7a84}
.tg-report .sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:5px;box-shadow:inset 0 0 0 1px rgba(255,255,255,.18)}
.tg-ok{color:#7ccf8a}.tg-rep{color:#e5c07b}.tg-fail{color:#ef6b6b;font-weight:600}
.tg-cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media (max-width:980px){.tg-cols{grid-template-columns:1fr}}
.tg-repairs{margin:10px 0 0;padding-left:16px;color:#b8b8c0}
"""


def preview_html(theme: dict, *, compact: bool = False) -> str:
    """A self-contained HTML page previewing the theme dark | light side by side: surfaces,
    text tiers, accent + kit buttons, alerts + badges (status), a card with a stat, a form field
    with a focus ring, chart series, the brand marks, and the contrast table. Uses the DS's own
    ``.pl-*`` class names and ``--pl-*`` vars, so inside an artifact sandbox that ships the kit
    it renders the REAL components; the ``:where()`` fallbacks (zero specificity) style it
    fully when the kit is absent.

    ``compact=True`` drops the contrast table and minifies the CSS — the version handed to
    ``show_artifact`` inline (the full page is served from the plugin's preview route)."""
    dark, light, base = theme["dark"], theme["light"], theme.get("base", {})
    roles = theme.get("roles", {})

    def decls(m: dict) -> str:
        return "".join(f"{k}:{v};" for k, v in m.items())

    extras = theme.get("defaults", {})
    base_css = _PREVIEW_CSS
    if compact:
        # The artifact sandbox links the DS kit, so the :where() kit fallbacks can go too.
        base_css = "".join(
            ln.strip() for ln in _PREVIEW_CSS.splitlines()
            if ln.strip() and not ln.startswith((".tg-report", ".tg-ok", ".tg-cols", ".tg-repairs", ":where(")) and "tg-cols" not in ln
        )
    css = base_css + (
        f".tg-root{{{decls(extras)}{decls(base)}}}"
        f".tg-dark{{color-scheme:dark;{decls(dark)}}}"
        f".tg-light{{color-scheme:light;{decls(light)}}}"
    )
    surfaces = [v for v, r in roles.items() if r in ("ground", "surface") and v in dark]
    series = [v for v, r in roles.items() if r == "chart-series" and v in dark]
    marks = [v for v, r in roles.items() if r == "brand-mark" and v in dark]
    statuses = [v for v, r in roles.items() if r == "status" and v in dark]

    def status_kind(v: str) -> str:
        for k in ("success", "warning", "error", "info"):
            if k in v:
                return k
        return "info"

    heights = [82, 58, 70, 44, 90, 36, 64, 52, 76, 48]

    def panel(mode: str) -> str:
        surf = "".join(
            f'<div class="tg-surf" style="background:var({v})"><span>{_esc(v.removeprefix("--pl-color-"))}</span></div>'
            for v in surfaces[:5]
        )
        msgs = {"success": ("Deployed", "Build 4812 is live."), "warning": ("Quota at 82%", "Consider upgrading soon."),
                "error": ("Sync failed", "Token expired — reconnect."), "info": ("New release", "v2.4 notes are ready.")}
        alerts = "".join(
            f'<div class="pl-alert pl-alert--{status_kind(v)}"><i class="tg-dotc" style="background:var({v})"></i>'
            f'<div class="pl-alert__body"><div class="pl-alert__title">{msgs[status_kind(v)][0]}</div>'
            f'<div class="pl-alert__text">{msgs[status_kind(v)][1]}</div></div></div>'
            for v in statuses[:4]
        )
        badges = '<span class="pl-badge tg-badge--accent">brand</span>' + "".join(
            f'<span class="pl-badge pl-badge--{status_kind(v)}">{status_kind(v)}</span>' for v in statuses[:4]
        ) + '<span class="pl-badge">neutral</span>'
        bars = "".join(
            f'<div class="tg-b" style="height:{heights[i % len(heights)]}%;background:var({v})"><span>{i + 1}</span></div>'
            for i, v in enumerate(series)
        )
        mark_strip = "".join(f'<i style="background:var({v})"></i>' for v in marks)
        return f"""
<section class="tg-panel tg-{mode}" aria-label="{mode} theme">
  <div class="tg-mode"><b>{mode}</b><span class="tg-cap">bg {_esc(theme[mode].get('--pl-color-bg', ''))} · accent {_esc(theme[mode].get('--pl-color-accent', ''))}</span></div>
  <h2 class="tg-h">Ship the dashboard today</h2>
  <p class="tg-p">Secondary copy sits in fg-muted — long enough to judge a paragraph's texture against the ground.</p>
  <p class="tg-cap">Tertiary caption · updated 4 min ago · <a class="tg-link" href="#">Read the changelog</a></p>
  <div class="tg-lbl">Surfaces</div>
  <div class="tg-surfaces">{surf}</div>
  <div class="tg-lbl">Actions</div>
  <div class="tg-row">
    <button class="tg-btn tg-btn--accent" type="button">Create project</button>
    <button class="tg-btn tg-btn--accent is-hover" type="button">Hover</button>
    <button class="pl-btn pl-btn--primary" type="button">Primary (kit)</button>
    <button class="pl-btn" type="button">Secondary</button>
    <button class="pl-btn pl-btn--ghost" type="button">Ghost</button>
  </div>
  <div class="tg-lbl">Status</div>
  <div class="tg-alerts">{alerts}</div>
  <div class="tg-row" style="margin-top:10px">{badges}</div>
  <div class="tg-lbl">Card &amp; form</div>
  <div class="tg-two">
    <div class="pl-card tg-card">
      <div class="tg-cap">Monthly active</div>
      <div class="tg-stat">48,210</div>
      <div class="tg-delta">▲ 12.4% vs last month</div>
      <div class="tg-bar"><i></i></div>
    </div>
    <div class="pl-card tg-card">
      <label class="pl-field__label" for="f-{mode}">Workspace name</label>
      <input id="f-{mode}" class="pl-input tg-focus" value="Acme Studio">
      <div class="tg-help">Focus ring uses <code>--pl-color-focus</code>.</div>
      <input class="pl-input" style="margin-top:8px" placeholder="Placeholder text">
    </div>
  </div>
  <div class="tg-lbl">Chart series</div>
  <div class="tg-chart"><div class="tg-yax"><span>100</span><span>50</span><span>0</span></div>{bars}</div>
  {'<div class="tg-lbl">Brand marks</div><div class="tg-marks">' + mark_strip + '</div>' if marks else ''}
</section>"""

    def report_rows(mode: str) -> str:
        # One row per token: its WORST pairing (every pair is in the .theme.json).
        worst: dict[str, dict] = {}
        for r in theme.get("contrast", []):
            if r["theme"] != mode:
                continue
            w = worst.get(r["fg"])
            rank = {"fail": 2, "repaired": 1, "pass": 0}
            if w is None:
                worst[r["fg"]] = dict(r, n=1, st=r["status"])
                continue
            w["n"] += 1
            if rank[r["status"]] > rank[w["st"]]:
                w["st"] = r["status"]
            if r["ratio"] / r["need"] < w["ratio"] / w["need"]:
                w.update({k: r[k] for k in ("bg", "bg_value", "ratio", "need")})
        rows = []
        for r in worst.values():
            cls = {"pass": "tg-ok", "repaired": "tg-rep", "fail": "tg-fail"}[r["st"]]
            rows.append(
                f'<tr><td><i class="sw" style="background:{_esc(r["fg_value"])}"></i>{_esc(r["fg"].removeprefix("--pl-color-"))}</td>'
                f'<td><i class="sw" style="background:{_esc(r["bg_value"])}"></i>{_esc(r["bg"].removeprefix("--pl-color-"))}'
                f'{" <small>(worst of " + str(r["n"]) + ")</small>" if r["n"] > 1 else ""}</td>'
                f'<td>{r["ratio"]:.2f}</td><td>{r["need"]:g}</td><td class="{cls}">{r["st"]}</td></tr>'
            )
        return "".join(rows)

    reps = theme.get("adjustments", []) + theme.get("repairs", [])
    rep_html = "".join(
        f"<li>{_esc(r['theme'])} · <b>{_esc(str(r['var']).removeprefix('--pl-color-'))}</b> {_esc(r['from'])} → {_esc(r['to'])} — {_esc(r['reason'])}</li>"
        for r in reps[:40]
    ) or "<li>none — every pair cleared on the first pass</li>"
    s = theme.get("seeds", {})
    seeds = "".join(
        f'<span class="tg-seed"><i style="background:{_esc(s[k])}"></i>{k} {_esc(s[k])}</span>'
        for k in ("primary", "secondary", "neutral") if s.get(k)
    )
    n = len(theme.get("contrast", []))
    n_fail = sum(r["status"] == "fail" for r in theme.get("contrast", []))
    n_rep = sum(r["status"] == "repaired" for r in theme.get("contrast", []))
    verdict = f"{n} pairs checked · {n - n_fail - n_rep} pass · {n_rep} repaired · {n_fail} fail"
    # Defense in depth: nothing in the stylesheet may close the <style> element.
    css = css.replace("</", "<\\/")
    if compact:
        # One panel's markup, cloned for the light side (the artifact sandbox runs scripts) —
        # halves what the agent has to echo into show_artifact.
        cap = f'bg {_esc(light.get("--pl-color-bg", ""))} · accent {_esc(light.get("--pl-color-accent", ""))}'
        light_shell = f'<section class="tg-panel tg-light" aria-label="light theme" data-clone="{cap}"></section>'
        return (
            f'<style>{css}</style><div class="tg-root"><header class="tg-top"><h1>{_esc(theme.get("name", "brand"))} — generated theme</h1>'
            f'<div class="tg-meta">{seeds}<span>{_esc(verdict)}</span></div></header>'
            + re.sub(r">\s+<", "><", f'<main class="tg-grid">{panel("dark")}{light_shell}</main></div>')
            + _CLONE_JS
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(theme.get('name', 'brand'))} theme preview</title>
<style>{css}</style></head>
<body class="tg-root">
<header class="tg-top"><h1>{_esc(theme.get('name', 'brand'))} — generated theme</h1>
<div class="tg-meta">{seeds}<span>{_esc(verdict)}</span></div></header>
<main class="tg-grid">{panel('dark')}{panel('light')}</main>
<section class="tg-report"><h2>Contrast (WCAG 2.x) — body text 4.5:1, tertiary/UI 3:1</h2>
<div class="tg-cols"><div><table><tr><th>dark · token</th><th>worst ground</th><th>ratio</th><th>need</th><th></th></tr>{report_rows('dark')}</table></div>
<div><table><tr><th>light · token</th><th>worst ground</th><th>ratio</th><th>need</th><th></th></tr>{report_rows('light')}</table></div></div>
<h2 style="margin-top:14px">Adjustments &amp; repairs</h2><ul class="tg-repairs">{rep_html}</ul></section>
</body></html>"""


_CLONE_JS = (
    "<script>for(const s of document.querySelectorAll('[data-clone]')){"
    "s.innerHTML=document.querySelector('.tg-dark').innerHTML.replaceAll('f-dark','f-light');"
    "s.querySelector('.tg-mode b').textContent='light';"
    "s.querySelector('.tg-mode .tg-cap').textContent=s.dataset.clone}</script>"
)


def non_color_defaults(contract: dict) -> dict:
    """The contract's non-color tokens (radius, fonts, space…) — the preview sets them so it is
    fully styled even where the DS stylesheet isn't loaded."""
    return {k: v for k, v in contract.get("dark", {}).items() if not k.startswith("--pl-color-") and "var(" not in v}
