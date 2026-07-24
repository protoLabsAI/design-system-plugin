"""Theme-designer engine — LCH color science for agent-driven console theming.

Ported from the operator's proto2 color playground (chroma.js `/tokens/color`):
perceptually-uniform 11-step scales (LCH lightness ramp 95→15 through the base),
classic harmonies by LCH hue rotation, full semantic palettes, and WCAG 2.x
contrast baked into everything — an agent should never *suggest* an illegible
theme, let alone apply one.

Pure logic lives here (unit-testable, no protoAgent imports); the `theme_apply`
persistence seam is injected by ``__init__`` so this module stays host-agnostic.
Color math via the in-repo ``colorcore`` module (pure Python, D65 CIELAB/LCH +
WCAG luminance) — plugin tools import in the HOST process, where the frozen
desktop app can never grow a wheel, so a pip dependency was never viable here.
"""

from __future__ import annotations

import importlib.util as _ilu
from pathlib import Path as _Path

_spec = _ilu.spec_from_file_location("design_system_colorcore", _Path(__file__).resolve().parent / "colorcore.py")
cc = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(cc)

# The scale positions proto2 used (Tailwind-style stops, 11 steps, 50→950).
SCALE_STOPS = (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)

# proto2's fixed semantic hues — scaled per-palette rather than invented per call.
SEMANTIC_BASES = {"success": "#10b981", "warning": "#f59e0b", "error": "#ef4444", "neutral": "#6b7280"}

HARMONY_ROTATIONS = {
    "complementary": (0, 180),
    "triadic": (0, 120, -120),
    "analogous": (0, 30, -30),
}


def _norm_hex(color: str) -> str:
    return cc.to_hex(cc.parse_hex(color))


def scale(base: str, steps: int = 11) -> list[str]:
    """A perceptually-uniform lightness scale through ``base`` — proto2's recipe:
    interpolate base@L95 → base → base@L15 in LCH and sample ``steps`` colors.
    Light end first (step 50) → dark end last (step 950)."""
    L, C, h = cc.hex_to_lch(base)
    stops = [(95.0, C, h), (L, C, h), (15.0, C, h)]
    return [cc.lch_to_hex(cc.interp_lch(stops, i / (steps - 1))) for i in range(steps)]


def harmony(base: str, kind: str) -> list[str]:
    """Rotate the base hue in LCH by the classic offsets. ``kind`` ∈
    complementary | triadic | analogous."""
    if kind not in HARMONY_ROTATIONS:
        raise ValueError(f"unknown harmony {kind!r} — pick one of {sorted(HARMONY_ROTATIONS)}")
    L, C, h = cc.hex_to_lch(base)
    return [cc.lch_to_hex((L, C, (h + rot) % 360)) for rot in HARMONY_ROTATIONS[kind]]


def contrast(fg: str, bg: str) -> dict:
    """WCAG 2.x contrast ratio + level verdicts for a color pair."""
    lf = cc.wcag_luminance(cc.parse_hex(fg))
    lb = cc.wcag_luminance(cc.parse_hex(bg))
    ratio = (max(lf, lb) + 0.05) / (min(lf, lb) + 0.05)
    return {
        "ratio": round(ratio, 2),
        "aa_normal": ratio >= 4.5,
        "aa_large": ratio >= 3.0,
        "aaa_normal": ratio >= 7.0,
    }


def palette(base: str, kind: str = "complementary") -> dict:
    """A full design-system palette from one base color: harmony-derived primary/
    secondary/accent + proto2's fixed semantic hues, every family as an 11-step
    scale keyed by the Tailwind-style stop."""
    fam = harmony(base, kind)
    accent_seed = fam[2] if len(fam) > 2 else fam[-1]
    families = {
        "primary": scale(fam[0]),
        "secondary": scale(fam[1]),
        "accent": scale(accent_seed),
        **{name: scale(hexv) for name, hexv in SEMANTIC_BASES.items()},
    }
    return {
        "base": _norm_hex(base),
        "harmony": kind,
        "scales": {name: dict(zip(SCALE_STOPS, s)) for name, s in families.items()},
    }


def overrides_for(pal: dict, mode: str = "dark") -> dict:
    """Map a ``palette()`` result onto the console's ``--pl-*`` override keys for
    one mode — the exact shape the ThemePanel persists ({mode, overrides}). The
    mapping mirrors the design package's semantic roles; every fg/bg pair is
    contrast-checked and the report rides along so callers can refuse or warn."""
    s = pal["scales"]
    dark = mode == "dark"

    def stop(family: str, dark_stop: int, light_stop: int) -> str:
        return s[family][dark_stop if dark else light_stop]

    ov = {
        "--pl-color-bg": stop("neutral", 950, 50),
        "--pl-color-bg-raised": stop("neutral", 900, 100),
        "--pl-color-bg-subtle": stop("neutral", 900, 100),
        "--pl-color-bg-hover": stop("neutral", 800, 200),
        "--pl-color-fg": stop("neutral", 50, 950),
        "--pl-color-fg-muted": stop("neutral", 300, 600),
        "--pl-color-fg-subtle": stop("neutral", 400, 500),
        "--pl-color-border": stop("neutral", 800, 200),
        "--pl-color-border-strong": stop("neutral", 700, 300),
        "--pl-color-accent": stop("accent", 400, 600),
        "--pl-color-accent-hover": stop("accent", 300, 700),
        "--pl-color-fg-on-accent": stop("neutral", 950, 50),
        "--pl-color-focus": stop("accent", 400, 600),
        "--pl-color-status-success": stop("success", 400, 600),
        "--pl-color-status-warning": stop("warning", 400, 600),
        "--pl-color-status-error": stop("error", 400, 600),
        "--pl-color-status-info": stop("secondary", 400, 600),
    }
    report = {
        "fg/bg": contrast(ov["--pl-color-fg"], ov["--pl-color-bg"]),
        "fg-muted/bg": contrast(ov["--pl-color-fg-muted"], ov["--pl-color-bg"]),
        "fg-on-accent/accent": contrast(ov["--pl-color-fg-on-accent"], ov["--pl-color-accent"]),
        "fg/bg-raised": contrast(ov["--pl-color-fg"], ov["--pl-color-bg-raised"]),
    }
    return {"mode": mode, "overrides": ov, "contrast": report}


def validate_overrides(overrides: dict) -> tuple[dict, list[str]]:
    """Sanity-gate a raw override map before it is persisted: every key must be a
    ``--pl-*`` custom property, every value a parseable color (non-color tokens
    like radii are passed through untouched with a note). Returns (clean, warnings);
    raises ValueError only for structurally invalid keys."""
    clean: dict = {}
    warnings: list[str] = []
    for k, v in overrides.items():
        if not isinstance(k, str) or not k.startswith("--pl-"):
            raise ValueError(f"override key {k!r} is not a --pl-* custom property")
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"override {k} has a non-string/empty value")
        try:
            cc.parse_hex(v)
        except ValueError:  # non-hex tokens (radius, fonts, rgb()/oklch() strings) pass through
            warnings.append(f"{k}: {v!r} is not a parseable hex color — passed through unvalidated")
        clean[k] = v.strip()
    fg, bg = clean.get("--pl-color-fg"), clean.get("--pl-color-bg")
    if fg and bg:
        rep = contrast(fg, bg)
        if not rep["aa_normal"]:
            warnings.append(f"fg/bg contrast {rep['ratio']}:1 FAILS WCAG AA (4.5:1) — theme applied anyway; consider fixing")
    return clean, warnings
