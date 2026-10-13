"""Pure color-theme serialization. DTCG 2025.10 colors; legacy Style Dictionary.

No filesystem/network access or color generation. The caller supplies the existing
vocabulary's color parser so the serializer shares its supported literal formats.
"""
from __future__ import annotations

import json
import math
import re

_NAME = re.compile(r"--pl-[A-Za-z0-9_-]+\Z")
_FAMILY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_STOPS = {str(n) for n in (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)}
_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?"
_COMPONENT = re.compile(rf"({_NUMBER})(%|deg|grad|rad|turn)?\Z", re.I)


def _literal_syntax(value: str) -> bool:
    """Validate the supported CSS grammar before the audit parser normalizes it.

    The audit parser tolerates missing percentages and mixed separators. Export
    preserves literals in CSS, so accepting those would emit invalid declarations.
    This checks syntax only; color conversion stays in the shared vocabulary.
    """
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}|[a-zA-Z]+", value):
        return True  # The shared parser checks hex length and known names.
    match = re.fullmatch(r"([a-z]+)\(([^()]*)\)", value, re.I)
    if not match:
        return False
    fn, body = match[1].lower(), match[2]
    legacy = "," in body

    def component(text, units=("", "%")):
        if text.lower() == "none":
            return not legacy
        number = _COMPONENT.fullmatch(text)
        return bool(number and (number[2] or "").lower() in units and math.isfinite(float(number[1])))

    if legacy:
        if fn not in ("rgb", "rgba", "hsl", "hsla") or "/" in body:
            return False
        parts = [part.strip() for part in body.split(",")]
        if len(parts) not in (3, 4):
            return False
        if len(parts) == 4 and not component(parts.pop()):
            return False
        if fn in ("rgb", "rgba") and len({part.endswith("%") for part in parts}) != 1:
            return False
    else:
        sections = body.split("/")
        if len(sections) > 2 or (len(sections) == 2 and not component(sections[1].strip())):
            return False
        parts = sections[0].split()
        if fn == "color":
            if not parts or parts.pop(0).lower() not in ("srgb", "srgb-linear"):
                return False
        if len(parts) != 3:
            return False
    hue = ("", "deg", "grad", "rad", "turn")
    if fn in ("hsl", "hsla"):
        units = ("%",) if legacy else ("", "%")
        return component(parts[0], hue) and all(component(p, units) for p in parts[1:])
    if fn in ("lch", "oklch"):
        return all(component(p) for p in parts[:2]) and component(parts[2], hue)
    return fn in ("rgb", "rgba", "lab", "oklab", "color") and all(component(p) for p in parts)


def export_theme(source_json: str, format: str, parse_color) -> str:
    """Serialize a palette result, {mode, overrides}, or {dark, light} maps.

    Export is deliberately color-only: unsupported values produce an error rather
    than disappearing or being mislabeled as colors. CSS retains supplied literal
    spellings; DTCG stores sRGB components, alpha and original CSS in an extension.
    """
    if format not in ("dtcg", "css", "style-dictionary"):
        raise ValueError("format must be dtcg, css, or style-dictionary")
    source = json.loads(source_json)
    if not isinstance(source, dict) or not source:
        raise ValueError("source must be a non-empty palette result or theme object")

    def color(value):
        if not isinstance(value, str) or not value.strip() or re.search(r"[;{}<>\r\n]", value):
            raise ValueError("each value must be a literal CSS color")
        if not _literal_syntax(value.strip()):
            raise ValueError(f"unsupported or malformed color {value!r}")
        rgba = parse_color(value)
        if rgba is None or not all(math.isfinite(v) for v in rgba):
            raise ValueError(f"unsupported color {value!r}; resolve references before export")
        return value.strip(), rgba

    def overrides(raw):
        if not isinstance(raw, dict) or not raw:
            raise ValueError("overrides must be a non-empty map of --pl-* names to colors")
        out = {}
        for name, value in raw.items():
            if not _NAME.fullmatch(name):
                raise ValueError(f"invalid custom-property name {name!r}")
            out[name] = color(value)
        return out

    palette = source.get("palette")
    scales = {}
    if palette is not None:
        if not isinstance(palette, dict) or not isinstance(palette.get("scales"), dict) or not palette["scales"]:
            raise ValueError("palette.scales must be a non-empty family map")
        for family, stops in palette["scales"].items():
            if not _FAMILY.fullmatch(family) or not isinstance(stops, dict) or not stops:
                raise ValueError("palette families must contain named color stops")
            scales[family] = {}
            for stop, value in stops.items():
                if stop not in _STOPS:
                    raise ValueError(f"invalid palette stop {stop!r}; expected 50, 100, …, 950")
                scales[family][stop] = color(value)

    if "overrides" in source:
        if "dark" in source or "light" in source:
            raise ValueError("use either {mode, overrides} or {dark, light}, not both")
        mode = source.get("mode", "dark")
        if mode not in ("dark", "light"):
            raise ValueError("mode must be dark or light")
        modes = {mode: overrides(source["overrides"])}
    else:
        modes = {mode: overrides(source[mode]) for mode in ("dark", "light") if mode in source}
        if not modes:
            raise ValueError("source needs overrides or dark/light color maps")

    if format == "css":
        blocks = []
        for index, (mode, values) in enumerate(modes.items()):
            declarations = {name: value[0] for name, value in values.items()}
            for family, stops in scales.items():
                for stop, value in stops.items():
                    name = f"--pl-palette-{family}-{stop}"
                    if name in declarations:
                        raise ValueError(f"palette export collides with override {name}")
                    declarations[name] = value[0]
            selector = ":root" if index == 0 else f'[data-theme="{mode}"]'
            blocks.append(selector + " {\n" + "\n".join(f"  {name}: {value};" for name, value in declarations.items()) + "\n}")
        return "\n\n".join(blocks) + "\n"

    def token(value, mode):
        literal, rgba = value
        if format == "style-dictionary":
            return {"value": literal, "type": "color", "$extensions": {"mode": mode}}
        return {
            "$type": "color",
            "$value": {"colorSpace": "srgb", "components": list(rgba[:3]), "alpha": rgba[3]},
            "$extensions": {"studio.protolabs": {"mode": mode, "css": literal}},
        }

    result = {}
    for mode, values in modes.items():
        tree = {"semantic": {name: token(value, mode) for name, value in values.items()}}
        if scales:
            tree["palette"] = {family: {stop: token(value, mode) for stop, value in stops.items()} for family, stops in scales.items()}
        result[mode] = tree
    return json.dumps(result, indent=2, allow_nan=False)
