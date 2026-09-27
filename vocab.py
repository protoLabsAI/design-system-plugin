"""Token vocabulary — the design system's values as something an auditor can QUERY.

``tokens.py`` turns ``tokens.css`` into renderable sections for a person to browse. This
module turns the same file into lookups a rule engine needs:

* every var name the DS publishes (``is_known``), with per-theme values;
* reverse maps value → vars (``vars_for_value``) — "this literal IS ``--pl-color-accent``";
* ``nearest_color`` — the closest color token by CIEDE2000, for literals that are *almost*
  a token (the usual shape of drift: ``#9a86f0`` next to ``#9b87f2``);
* length scales (space / radius / font-size) and the shadow set, with ``nearest_length`` —
  and, just as important, which scales the DS **doesn't** have (``missing_scales``), so an
  auditor reports "the DS has no type scale" ONCE instead of 266 unfixable findings.

Pure: no I/O, no network, no protoAgent imports. Callers hand in CSS/JSON text — the repo
auditor reads files, ``__init__`` fetches from GitHub, a URL auditor can feed computed styles.

Color parsing (``parse_color``) accepts every CSS form a consumer writes: ``#rgb``/``#rgba``/
``#rrggbb``/``#rrggbbaa``, ``rgb()/rgba()`` (comma or space syntax, % or 0-255), ``hsl()/
hsla()``, ``oklch()``/``oklab()`` (the DS itself ships status colors as oklch), ``lab()/
lch()``, ``color(srgb …)`` and the common named colors. Anything carrying ``var()``/``calc()``
is not a literal and parses to ``None``.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import math
import re
from pathlib import Path as _Path


def _load(filename: str):
    spec = _ilu.spec_from_file_location(f"design_system_vocab_{_Path(filename).stem}", _Path(__file__).resolve().parent / filename)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load("colorcore.py")
_tokens = _load("tokens.py")

RGBA = tuple  # (r, g, b, a) floats 0..1

# The CSS named colors people actually type. Not all 148 — a name outside this list is far
# more likely to be an identifier than a color, and false positives are what kill an audit.
NAMED_COLORS: dict[str, str] = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000", "blue": "#0000ff",
    "yellow": "#ffff00", "orange": "#ffa500", "purple": "#800080", "gray": "#808080", "grey": "#808080",
    "silver": "#c0c0c0", "maroon": "#800000", "olive": "#808000", "lime": "#00ff00", "aqua": "#00ffff",
    "cyan": "#00ffff", "teal": "#008080", "navy": "#000080", "fuchsia": "#ff00ff", "magenta": "#ff00ff",
    "pink": "#ffc0cb", "brown": "#a52a2a", "gold": "#ffd700", "indigo": "#4b0082", "violet": "#ee82ee",
    "crimson": "#dc143c", "coral": "#ff7f50", "tomato": "#ff6347", "salmon": "#fa8072", "khaki": "#f0e68c",
    "tan": "#d2b48c", "beige": "#f5f5dc", "ivory": "#fffff0", "lavender": "#e6e6fa", "plum": "#dda0dd",
    "orchid": "#da70d6", "turquoise": "#40e0d0", "slategray": "#708090", "slategrey": "#708090",
    "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9", "lightgray": "#d3d3d3", "lightgrey": "#d3d3d3",
    "whitesmoke": "#f5f5f5", "gainsboro": "#dcdcdc", "dimgray": "#696969", "dimgrey": "#696969",
    "rebeccapurple": "#663399", "darkred": "#8b0000", "darkblue": "#00008b", "darkgreen": "#006400",
    "lightblue": "#add8e6", "lightgreen": "#90ee90", "skyblue": "#87ceeb", "steelblue": "#4682b4",
    "royalblue": "#4169e1", "dodgerblue": "#1e90ff", "hotpink": "#ff69b4", "deeppink": "#ff1493",
    "goldenrod": "#daa520", "firebrick": "#b22222", "seagreen": "#2e8b57", "limegreen": "#32cd32",
    "orangered": "#ff4500", "chocolate": "#d2691e", "sienna": "#a0522d", "midnightblue": "#191970",
    "transparent": "#00000000",
}

_HEX_FULL = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_FUNC = re.compile(r"^(rgba?|hsla?|oklch|oklab|lab|lch|color)\((.*)\)$", re.DOTALL | re.IGNORECASE)
_NUM = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(%|deg|rad|grad|turn)?$", re.IGNORECASE)
_LENGTH = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))(px|rem)?$", re.IGNORECASE)


# ── color parsing ─────────────────────────────────────────────────────────────


def _hex_rgba(h: str) -> RGBA:
    h = h.lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h)
    vals = [int(h[i : i + 2], 16) / 255.0 for i in range(0, len(h), 2)]
    if len(vals) == 3:
        vals.append(1.0)
    return tuple(vals)


def _num(tok: str, pct_scale: float = 1.0, angle: bool = False) -> float | None:
    """One CSS numeric component. ``pct_scale`` maps 100% onto the channel's range."""
    if tok.lower() == "none":
        return 0.0
    m = _NUM.match(tok)
    if not m:
        return None
    v, unit = float(m.group(1)), (m.group(2) or "").lower()
    if unit == "%":
        return v / 100.0 * pct_scale
    if angle:
        return {"": v, "deg": v, "rad": math.degrees(v), "grad": v * 0.9, "turn": v * 360}[unit]
    return v if not unit else None


def _hsl_to_rgb(h: float, s: float, l: float) -> tuple[float, float, float]:
    h = (h % 360) / 360.0
    s, l = max(0.0, min(1.0, s)), max(0.0, min(1.0, l))
    if s == 0:
        return (l, l, l)
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q

    def hue(t: float) -> float:
        t %= 1.0
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    return (hue(h + 1 / 3), hue(h), hue(h - 1 / 3))


def parse_color(text: str) -> RGBA | None:
    """Any CSS color literal → ``(r, g, b, a)`` floats 0..1 (sRGB, clamped), else ``None``.

    ``None`` for anything that isn't a literal: ``var()``, ``calc()``, ``currentColor``,
    ``inherit``, a bare identifier, a malformed function."""
    v = (text or "").strip()
    if not v:
        return None
    low = v.lower()
    if "var(" in low or "calc(" in low or "env(" in low:
        return None
    if low in NAMED_COLORS:
        return _hex_rgba(NAMED_COLORS[low])
    if _HEX_FULL.match(v):
        return _hex_rgba(v)
    m = _FUNC.match(v)
    if not m:
        return None
    fn, body = m.group(1).lower(), m.group(2)
    if fn == "color":
        parts = body.replace("/", " / ").split()
        if not parts or parts[0].lower() not in ("srgb", "srgb-linear"):
            return None
        parts = parts[1:]
    else:
        parts = body.replace(",", " ").replace("/", " / ").split()
    alpha = 1.0
    if "/" in parts:
        i = parts.index("/")
        if i + 2 != len(parts):
            return None
        a = _num(parts[i + 1])
        if a is None:
            return None
        alpha, parts = a, parts[:i]
    elif len(parts) == 4:
        a = _num(parts[3])
        if a is None:
            return None
        alpha, parts = a, parts[:3]
    if len(parts) != 3:
        return None
    try:
        if fn in ("rgb", "rgba"):
            ch = [_num(p, 255.0) for p in parts]
            if None in ch:
                return None
            rgb = tuple(c / 255.0 for c in ch)
        elif fn in ("hsl", "hsla"):
            h, s, l = _num(parts[0], angle=True), _num(parts[1]), _num(parts[2])
            if None in (h, s, l):
                return None
            # hsl() saturation/lightness are %-only in the legacy syntax; a bare number is %.
            if not parts[1].endswith("%"):
                s = s / 100.0
            if not parts[2].endswith("%"):
                l = l / 100.0
            rgb = _hsl_to_rgb(h, s, l)
        elif fn == "oklch":
            L, C, h = _num(parts[0]), _num(parts[1], 0.4), _num(parts[2], angle=True)
            if None in (L, C, h):
                return None
            rgb = cc.oklch_to_rgb((L, C, h))
        elif fn == "oklab":
            L, a, b = _num(parts[0]), _num(parts[1], 0.4), _num(parts[2], 0.4)
            if None in (L, a, b):
                return None
            rgb = cc.oklab_to_rgb((L, a, b))
        elif fn in ("lab", "lch"):
            L = _num(parts[0], 100.0)
            if fn == "lab":
                a, b = _num(parts[1], 125.0), _num(parts[2], 125.0)
                if None in (L, a, b):
                    return None
                C, h = math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360
            else:
                C, h = _num(parts[1], 150.0), _num(parts[2], angle=True)
                if None in (L, C, h):
                    return None
            rgb = cc.lch_to_rgb((L, C, h))
        else:  # color(srgb …)
            ch = [_num(p) for p in parts]
            if None in ch:
                return None
            rgb = tuple(ch)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    clamp = lambda c: max(0.0, min(1.0, float(c)))
    return (clamp(rgb[0]), clamp(rgb[1]), clamp(rgb[2]), clamp(alpha))


def normalize_color(text: str) -> str | None:
    """A color literal in canonical form: ``#rrggbb``, or ``#rrggbbaa`` when not opaque."""
    rgba = parse_color(text)
    if rgba is None:
        return None
    out = cc.to_hex(rgba[:3])
    if rgba[3] < 0.9995:
        out += f"{round(rgba[3] * 255):02x}"
    return out


def is_color(text: str) -> bool:
    return parse_color(text) is not None


def color_distance(c1: RGBA, c2: RGBA) -> tuple[float, float]:
    """(ΔE2000 of the RGB part, |Δalpha|)."""
    return cc.delta_e_2000(cc.rgb_to_lab(c1[:3]), cc.rgb_to_lab(c2[:3])), abs(c1[3] - c2[3])


# ── lengths ───────────────────────────────────────────────────────────────────


def parse_length(text: str, root_px: float = 16.0) -> float | None:
    """``12px`` / ``0.75rem`` / unitless ``0`` → px. ``em``/``%``/``vh``… are relative to
    context, not absolute sizes, and parse to ``None`` (an auditor cannot judge them)."""
    m = _LENGTH.match((text or "").strip())
    if not m:
        return None
    v, unit = float(m.group(1)), (m.group(2) or "").lower()
    if unit == "rem":
        return v * root_px
    if unit == "px":
        return v
    return 0.0 if v == 0 else None


def normalize_value(text: str) -> str:
    """Canonical text for comparing arbitrary token values: colors → hex, absolute
    lengths → ``Npx``, everything else whitespace-collapsed, lowercased, double-quoted."""
    v = " ".join((text or "").split())
    col = normalize_color(v)
    if col:
        return col
    px = parse_length(v)
    if px is not None:
        return f"{px:g}px"
    v = v.lower().replace("'", '"')
    v = re.sub(r"\s*,\s*", ", ", v)
    v = re.sub(r"\(\s+", "(", v)
    return re.sub(r"\s+\)", ")", v)


# ── the vocabulary ────────────────────────────────────────────────────────────

# Which token names form which scale. `[-.]` so the same patterns work for CSS var names
# (--pl-space-4) and dotted JSON paths (space.4) in the JSON-only fallback.
_SCALE_PATTERNS: dict[str, re.Pattern] = {
    "space": re.compile(r"(^|[-.])(space|spacing|gap)([-.]|$)", re.IGNORECASE),
    "radius": re.compile(r"radius|rounded", re.IGNORECASE),
    "font-size": re.compile(r"font[-.](\w+[-.])*size|(^|[-.])(text|font)[-.](2xs|xs|sm|base|md|lg|xl|[2-9]xl|display|body|caption|heading)$|type[-.]scale", re.IGNORECASE),
    "shadow": re.compile(r"shadow|elevation", re.IGNORECASE),
}
# A "scale" needs at least this many DISTINCT values to be worth snapping to. One radius
# token is a value, not a scale — off-scale findings against it would be noise.
MIN_SCALE = 2

SCALE_LABEL = {"space": "spacing", "radius": "border-radius", "font-size": "type (font-size)", "shadow": "elevation (shadow)"}


class Vocab:
    """Everything an auditor needs to know about the DS's tokens. Build with ``build_vocab``.

    A plain class, not a dataclass: plugin modules load by file path and are never entered
    in ``sys.modules``, which ``@dataclass`` needs to resolve postponed annotations."""

    def __init__(self, themes: dict[str, dict[str, str]] | None = None, extra_vars=(), source: str = "empty"):
        self.themes: dict[str, dict[str, str]] = themes or {}
        self.extra_vars: set[str] = set(extra_vars or ())
        self.source = source  # "css" | "json" | "empty"
        self.colors: dict[str, dict[str, RGBA]] = {}
        self.scales: dict[str, list[tuple[str, float]]] = {}
        self.shadows: list[tuple[str, str]] = []
        self._by_value: dict[str, set[str]] = {}
        self._color_cache: dict[str, list[dict]] = {}

    # ── names ────────────────────────────────────────────────────────────────
    @property
    def names(self) -> set[str]:
        out: set[str] = set()
        for vals in self.themes.values():
            out.update(vals)
        return out

    @property
    def prefix(self) -> str:
        """The DS's custom-property namespace (``--pl-``), read off its own names — the
        most common ``--xx-`` head. Empty for a JSON-only vocabulary."""
        heads: dict[str, int] = {}
        for n in self.names | self.extra_vars:
            m = re.match(r"(--[a-zA-Z0-9]+-)", n)
            if m:
                heads[m.group(1)] = heads.get(m.group(1), 0) + 1
        return max(heads, key=heads.get) if heads else ""

    def is_known(self, name: str) -> bool:
        return name in self.extra_vars or any(name in vals for vals in self.themes.values())

    def ref(self, name: str) -> str:
        """How a consumer writes the token: ``var(--pl-x)`` for a CSS var, the dotted path
        for a JSON-only vocabulary (which has no published var names to cite)."""
        return f"var({name})" if name.startswith("--") else name

    def value(self, name: str, theme: str | None = None) -> str | None:
        if theme:
            return self.themes.get(theme, {}).get(name)
        for vals in self.themes.values():
            if name in vals:
                return vals[name]
        return None

    def values(self, name: str) -> dict[str, str]:
        """{theme: raw value} for one token."""
        return {t: vals[name] for t, vals in self.themes.items() if name in vals}

    def is_themed(self, name: str) -> bool:
        return len({normalize_value(v) for v in self.values(name).values()}) > 1

    def close_names(self, name: str, n: int = 3) -> list[str]:
        import difflib

        return difflib.get_close_matches(name, sorted(self.names | self.extra_vars), n=n, cutoff=0.6)

    # ── values ───────────────────────────────────────────────────────────────
    def vars_for_value(self, value: str) -> list[str]:
        """Every token whose value (in any theme) equals ``value`` after normalization."""
        return _rank(self._by_value.get(normalize_value(value), set()))

    def value_matches(self, name: str, value: str, tol: float = 1.0) -> bool:
        """Does ``value`` equal ``name``'s value in ANY theme? Colors compare perceptually
        (ΔE2000 < ``tol`` and |Δα| < 0.02) because a DS that ships ``oklch()`` is routinely
        mirrored as the computed hex, which is the same color, not a stale one."""
        want_col = parse_color(value)
        for theme in self.themes:
            raw = self.resolved(name, theme)
            if raw is None:
                continue
            if want_col is not None:
                have = parse_color(raw)
                if have is not None:
                    de, da = color_distance(want_col, have)
                    if de < tol and da < 0.02:
                        return True
                    continue
            if normalize_value(raw) == normalize_value(value):
                return True
        return False

    def themes_matching(self, name: str, value: str, tol: float = 0.5) -> list[str]:
        """The themes in which ``name``'s value equals ``value`` (colors: ΔE < ``tol`` and the
        same alpha; anything else: normalized text). A themed token matched in only ONE theme
        is not a safe swap — the literal and the token differ in the other."""
        want = parse_color(value)
        out = []
        for theme in self.themes:
            raw = self.resolved(name, theme)
            if raw is None:
                continue
            have = parse_color(raw)
            if want is not None and have is not None:
                de, da = color_distance(want, have)
                if de < tol and da < 0.01:
                    out.append(theme)
            elif normalize_value(raw) == normalize_value(value):
                out.append(theme)
        return out

    def resolved(self, name: str, theme: str, depth: int = 0) -> str | None:
        """A token's value with ``var(--pl-other)`` references followed (within the theme)."""
        raw = self.themes.get(theme, {}).get(name)
        if raw is None or depth > 6:
            return raw
        m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,[^)]*)?\)", raw.strip())
        if m:
            return self.resolved(m.group(1), theme, depth + 1) or raw
        return raw

    # ── colors ───────────────────────────────────────────────────────────────
    def color_matches(self, value: str, k: int = 3) -> list[dict]:
        """The ``k`` nearest color tokens to a literal, best first:
        ``[{var, theme, delta_e, alpha_delta, distance}]``. ``distance`` = ΔE2000 +
        100·|Δα| — alpha counts, so ``rgba(0,0,0,.5)`` doesn't "match" opaque black.
        One entry per var (its best theme). Empty when the literal isn't a color or the DS
        has no color tokens."""
        rgba = parse_color(value)
        if rgba is None or not self.colors:
            return []
        key = normalize_color(value) or value
        if key not in self._color_cache:
            best: dict[str, dict] = {}
            for name, per_theme in self.colors.items():
                for theme, tok in per_theme.items():
                    de, da = color_distance(rgba, tok)
                    d = de + 100 * da
                    if name not in best or d < best[name]["distance"]:
                        best[name] = {"var": name, "theme": theme, "delta_e": round(de, 2), "alpha_delta": round(da, 3), "distance": round(d, 2)}
            self._color_cache[key] = sorted(best.values(), key=lambda r: (r["distance"], _rank_key(r["var"])))
        return self._color_cache[key][:k]

    def nearest_color(self, value: str) -> tuple[str, float] | None:
        """``(var, distance)`` of the closest color token (distance = ΔE2000 + 100·|Δα|;
        < 1 is the same color, < 5 close, > 10 a different color). ``None`` if not a color."""
        m = self.color_matches(value, 1)
        return (m[0]["var"], m[0]["distance"]) if m else None

    def exact_colors(self, value: str, tol: float = 0.5) -> list[str]:
        """Every color token equal to ``value`` (ΔE < ``tol``, same alpha), best-named first."""
        return _rank({m["var"] for m in self.color_matches(value, k=len(self.colors) or 1) if m["delta_e"] < tol and m["alpha_delta"] < 0.01})

    # ── lengths ──────────────────────────────────────────────────────────────
    def scale(self, kind: str) -> list[tuple[str, float]]:
        return self.scales.get(kind, [])

    def has_scale(self, kind: str) -> bool:
        if kind == "shadow":
            return len({n for n, _ in self.shadows}) >= MIN_SCALE
        return len({px for _, px in self.scale(kind)}) >= MIN_SCALE

    def missing_scales(self) -> list[str]:
        """Scale kinds the DS doesn't ship enough tokens for — DS gaps, not consumer bugs."""
        return [k for k in ("space", "radius", "font-size", "shadow") if not self.has_scale(k)]

    def nearest_length(self, kind: str, value: str | float) -> tuple[str, float, float] | None:
        """``(var, token_px, |Δpx|)`` for the closest token on ``kind``'s scale, or ``None``
        when the value isn't an absolute length or the DS has no tokens of that kind."""
        px = value if isinstance(value, (int, float)) else parse_length(str(value))
        entries = self.scale(kind)
        if px is None or not entries:
            return None
        name, tpx = min(entries, key=lambda e: (abs(abs(px) - e[1]), _rank_key(e[0])))
        return name, tpx, round(abs(abs(px) - tpx), 3)

    def shadow_for(self, value: str) -> str | None:
        want = normalize_value(value)
        for name, v in self.shadows:
            if v == want:
                return name
        return None

    def summary(self) -> dict:
        return {
            "source": self.source,
            "tokens": len(self.names),
            "themes": sorted(self.themes),
            "colors": len(self.colors),
            "scales": {k: [f"{n}={px:g}px" for n, px in self.scale(k)] for k in ("space", "radius", "font-size")},
            "shadows": list(dict.fromkeys(n for n, _ in self.shadows)),
            "missing_scales": self.missing_scales(),
        }


def _rank_key(name: str) -> tuple:
    """Prefer semantic tokens (bg/fg/accent/status…) over raw palette ones (brand-*) when
    several share a value — the DS wants consumers on the ROLE, not the swatch."""
    raw = 1 if re.search(r"brand|palette|primitive|raw|chart|focus|hover|glow|overlay", name) else 0
    return (raw, len(name), name)


def _rank(names) -> list[str]:
    return sorted(names, key=_rank_key)


def _flatten_json(obj, path: list[str] | None = None, out: dict[str, str] | None = None) -> dict[str, str]:
    path, out = path or [], out if out is not None else {}
    if isinstance(obj, dict):
        # Design-tokens-format leaf: {"$value": …} / {"value": …}.
        if "$value" in obj or ("value" in obj and not isinstance(obj.get("value"), dict)):
            val = obj.get("$value", obj.get("value"))
            if isinstance(val, (str, int, float)):
                out[".".join(path)] = str(val)
                return out
        for k, v in obj.items():
            if str(k).startswith("$"):
                continue
            _flatten_json(v, path + [str(k)], out)
    elif isinstance(obj, (str, int, float)) and not isinstance(obj, bool) and path:
        out[".".join(path)] = str(obj)
    return out


def build_vocab(tokens_css: str = "", tokens_json: str | dict | None = None, extra_vars=()) -> Vocab:
    """Build the vocabulary. ``tokens.css`` is the CONTRACT (the var names consumers write)
    and wins whenever it declares anything; ``tokens_json`` is only the fallback for a DS
    that publishes no built CSS — its names are dotted token paths, never invented var names.

    ``extra_vars`` are additional KNOWN names with no token value — e.g. component-level
    custom properties the DS's kit stylesheet defines (``--pl-appshell-rail-w``), which are
    legitimate to reference but aren't design tokens."""
    themes: dict[str, dict[str, str]] = {}
    source = "empty"
    if tokens_css and "--" in tokens_css:
        parsed = _tokens.parse_css(tokens_css)
        if parsed.get("dark"):
            themes, source = parsed, "css"
        else:
            # No bare :root (a `:host` / `.theme` / `html` token file): every declaration in
            # every block, one theme. Better than an empty vocabulary that flags every var.
            flat = dict(re.findall(r"(--[a-zA-Z0-9-]+)\s*:\s*([^;{}]+);", re.sub(r"/\*.*?\*/", " ", tokens_css, flags=re.DOTALL)))
            if flat:
                themes, source = {"default": {k: v.strip() for k, v in flat.items()}}, "css"
    if not themes and tokens_json:
        data = tokens_json
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = None
        flat = _flatten_json(data) if isinstance(data, (dict, list)) else {}
        if flat:
            themes, source = {"default": flat}, "json"

    v = Vocab(themes=themes, extra_vars=set(extra_vars or ()), source=source)
    for theme in themes:
        for name in themes[theme]:
            val = v.resolved(name, theme) or ""
            v._by_value.setdefault(normalize_value(val), set()).add(name)
            if _tokens.classify(val) == "color" or val.strip().lower() in NAMED_COLORS:
                rgba = parse_color(val)
                if rgba is not None:
                    v.colors.setdefault(name, {})[theme] = rgba

    # Scales are theme-independent in practice; read them off the first theme that has them.
    base = themes.get("dark") or themes.get("default") or {}
    for kind, pat in _SCALE_PATTERNS.items():
        if kind == "shadow":
            # Every theme's face of every shadow token — a light-mode shadow written by hand
            # is just as much a copy as a dark one.
            seen: set[tuple[str, str]] = set()
            for theme_vals in themes.values():
                for n, val in theme_vals.items():
                    if pat.search(n) and _tokens.classify(val) == "shadow" and (n, normalize_value(val)) not in seen:
                        seen.add((n, normalize_value(val)))
                        v.shadows.append((n, normalize_value(val)))
            continue
        entries = []
        for n, val in base.items():
            if not pat.search(n):
                continue
            px = parse_length(v.resolved(n, "dark" if "dark" in themes else "default") or "")
            if px is not None:
                entries.append((n, px))
        v.scales[kind] = sorted(entries, key=lambda e: (e[1], e[0]))
    return v


def kit_custom_properties(kit_css: str, prefix: str = "--pl-") -> set[str]:
    """Custom properties a DS stylesheet DEFINES (``--pl-foo: …``) — feed to
    ``build_vocab(extra_vars=…)`` so component-level vars aren't reported as unknown."""
    text = re.sub(r"/\*.*?\*/", " ", kit_css or "", flags=re.DOTALL)
    return {m for m in re.findall(r"(--[\w-]+)\s*:", text) if m.startswith(prefix)}
