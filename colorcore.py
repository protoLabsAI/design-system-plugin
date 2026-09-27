"""Pure-Python color core — sRGB ↔ CIELAB/LCH + WCAG luminance, no dependencies.

Replaces the coloraide dependency: plugin tools import IN the host process, and on
the frozen desktop app no wheel can ever land there (the managed runtime serves
execute_code children, not host imports — the requires_pip route was a dead end
for an in-process plugin). ~120 lines of D65 color science is the honest price.
"""

from __future__ import annotations

import re

_D65 = (0.95047, 1.0, 1.08883)
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def parse_hex(color: str) -> tuple[float, float, float]:
    """``#rgb``/``#rrggbb`` → sRGB floats 0..1. Raises ValueError on anything else."""
    m = _HEX_RE.match(color.strip())
    if not m:
        raise ValueError(f"not a hex color: {color!r}")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def wcag_luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _rgb_to_xyz(rgb):
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    return (
        0.4124564 * r + 0.3575761 * g + 0.1804375 * b,
        0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
        0.0193339 * r + 0.1191920 * g + 0.9503041 * b,
    )


def _xyz_to_rgb(xyz):
    x, y, z = xyz
    r = 3.2404542 * x - 1.5371385 * y - 0.4985314 * z
    g = -0.9692660 * x + 1.8760108 * y + 0.0415560 * z
    b = 0.0556434 * x - 0.2040259 * y + 1.0572252 * z
    return tuple(_linear_to_srgb(c) for c in (r, g, b))


def _f(t: float) -> float:
    return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116


def _finv(t: float) -> float:
    t3 = t ** 3
    return t3 if t3 > 216 / 24389 else (116 * t - 16) * 27 / 24389


def rgb_to_lch(rgb):
    """sRGB 0..1 → (L, C, h°) in CIELAB LCH."""
    import math

    x, y, z = _rgb_to_xyz(rgb)
    fx, fy, fz = _f(x / _D65[0]), _f(y / _D65[1]), _f(z / _D65[2])
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    b = 200 * (fy - fz)
    return (L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360)


def lch_to_rgb(lch):
    """(L, C, h°) → sRGB 0..1 (may be out of gamut — see fit_lch)."""
    import math

    L, C, h = lch
    a = C * math.cos(math.radians(h))
    b = C * math.sin(math.radians(h))
    fy = (L + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200
    xyz = (_finv(fx) * _D65[0], _finv(fy) * _D65[1], _finv(fz) * _D65[2])
    return _xyz_to_rgb(xyz)


def _in_gamut(rgb, eps: float = 1e-6) -> bool:
    return all(-eps <= c <= 1 + eps for c in rgb)


def fit_lch(lch) -> tuple[float, float, float]:
    """Gamut-fit by chroma reduction (binary search) — the lch-chroma method:
    keep L and h, shrink C until sRGB-representable."""
    L, C, h = lch
    if _in_gamut(lch_to_rgb((L, C, h))):
        return lch_to_rgb((L, C, h))
    lo, hi = 0.0, C
    for _ in range(24):
        mid = (lo + hi) / 2
        if _in_gamut(lch_to_rgb((L, mid, h))):
            lo = mid
        else:
            hi = mid
    return lch_to_rgb((L, lo, h))


def hex_to_lch(color: str):
    return rgb_to_lch(parse_hex(color))


def lch_to_hex(lch) -> str:
    return to_hex(fit_lch(lch))


def interp_lch(stops, t: float):
    """Piecewise-linear interpolation across LCH stops (shortest-arc hue), t in 0..1."""
    n = len(stops) - 1
    seg = min(int(t * n), n - 1)
    lt = t * n - seg
    (l1, c1, h1), (l2, c2, h2) = stops[seg], stops[seg + 1]
    dh = ((h2 - h1 + 180) % 360) - 180
    return (l1 + (l2 - l1) * lt, c1 + (c2 - c1) * lt, (h1 + dh * lt) % 360)


# ── additive: CIELAB + ΔE2000 + OKLab/OKLCH decode (used by vocab.py / audit.py) ──
# Kept separate from the LCH helpers above so theme.py's behaviour is untouched.


def rgb_to_lab(rgb) -> tuple[float, float, float]:
    """sRGB 0..1 → CIELAB (L, a, b), D65."""
    x, y, z = _rgb_to_xyz(rgb)
    fx, fy, fz = _f(x / _D65[0]), _f(y / _D65[1]), _f(z / _D65[2])
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e_76(lab1, lab2) -> float:
    """CIE76 — plain Euclidean distance in LAB. Cheap; overstates saturated differences."""
    return sum((p - q) ** 2 for p, q in zip(lab1, lab2)) ** 0.5


def delta_e_2000(lab1, lab2) -> float:
    """CIEDE2000 (Sharma et al. 2005 reference formulation). ~1 = just noticeable,
    <2 = same color to most eyes, >10 = clearly a different color."""
    import math

    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    cbar7 = ((c1 + c2) / 2) ** 7
    g = 0.5 * (1 - math.sqrt(cbar7 / (cbar7 + 25 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dLp = L2 - L1
    dCp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    Lbp = (L1 + L2) / 2
    Cbp = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    d_theta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7))
    sl = 1 + (0.015 * (Lbp - 50) ** 2) / math.sqrt(20 + (Lbp - 50) ** 2)
    sc = 1 + 0.045 * Cbp
    sh = 1 + 0.015 * Cbp * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc
    return math.sqrt(
        (dLp / sl) ** 2 + (dCp / sc) ** 2 + (dHp / sh) ** 2 + rt * (dCp / sc) * (dHp / sh)
    )


def oklab_to_rgb(lab) -> tuple[float, float, float]:
    """OKLab (L 0..1, a, b) → sRGB 0..1 (unclamped; clamp before hex)."""
    L, a, b = lab
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return tuple(_linear_to_srgb(c) if c >= 0 else -_linear_to_srgb(-c) for c in lin)  # type: ignore[return-value]


def oklch_to_rgb(lch) -> tuple[float, float, float]:
    """OKLCH (L 0..1, C, h°) → sRGB 0..1 (unclamped)."""
    import math

    L, C, h = lch
    return oklab_to_rgb((L, C * math.cos(math.radians(h)), C * math.sin(math.radians(h))))
