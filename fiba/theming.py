"""Country flags and team colours.

Two jobs, both self-contained so a report stays a single file with no external
requests:

* `flag_svg()` draws a small inline flag badge from a compact spec. At the size
  these render (about 16x11) fine detail is invisible, so the designs are
  deliberate simplifications: field colours and the major device, nothing more.

* `team_ink()` / `pick_pair()` choose chart colours. A country's flag colour is
  only usable as ink if it reads on white paper and if it is separable from the
  other team's colour, including for colourblind readers. Several real matchups
  fail that test (Netherlands and Croatia are both red/white/blue), so the pair
  is checked and the second team falls back to a known-good hue when it collides.
  The check is computed, not eyeballed.
"""
from __future__ import annotations

import html
import math

# ---------------------------------------------------------------------------
# Colour maths: sRGB -> OKLab, plus colour vision deficiency simulation.
# ---------------------------------------------------------------------------


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _to_oklab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


# Machado et al. (2009) dichromat simulation matrices, applied in linear RGB.
_CVD = {
    "protan": ((0.152286, 1.052583, -0.204868),
               (0.114503, 0.786281, 0.099216),
               (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968),
               (0.280085, 0.672501, 0.047413),
               (-0.011820, 0.042940, 0.968881)),
    "tritan": ((1.255528, -0.076749, -0.178779),
               (-0.078411, 0.930809, 0.147602),
               (0.004733, 0.691367, 0.303900)),
}


def _simulate(rgb: tuple[float, float, float], kind: str) -> tuple[float, float, float]:
    matrix = _CVD[kind]
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    out = tuple(row[0] * r + row[1] * g + row[2] * b for row in matrix)
    return tuple(_linear_to_srgb(c) for c in out)


def delta_e(hex_a: str, hex_b: str, kind: str | None = None) -> float:
    """OKLab distance x100, optionally through a CVD simulation."""
    a, b = _hex_to_rgb(hex_a), _hex_to_rgb(hex_b)
    if kind:
        a, b = _simulate(a, kind), _simulate(b, kind)
    la, aa, ba = _to_oklab(a)
    lb, ab, bb = _to_oklab(b)
    return 100.0 * math.dist((la, aa, ba), (lb, ab, bb))


def _from_oklab(lab: tuple[float, float, float]) -> str:
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return "#" + "".join(
        f"{round(255 * _linear_to_srgb(c)):02x}" for c in (r, g, bb)
    )


def shift_lightness(hex_value: str, delta: float) -> str:
    """Move a colour along OKLab lightness, keeping its hue.

    Used to separate two teams whose flag colours are close: a lighter or darker
    Swedish blue still reads as Sweden, whereas swapping it for a generic hue
    throws the identity away for nothing.
    """
    L, a, b = _to_oklab(_hex_to_rgb(hex_value))
    return _from_oklab((max(0.0, min(1.0, L + delta)), a, b))


def _relative_luminance(hex_value: str) -> float:
    r, g, b = (_srgb_to_linear(c) for c in _hex_to_rgb(hex_value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_value: str, background: str = "#ffffff") -> float:
    a, b = _relative_luminance(hex_value), _relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


# Thresholds. 15 is the normal-vision floor two series must clear; 8 is the CVD
# target. Marks must also reach 3:1 against the paper to be visible in print.
MIN_DELTA_NORMAL = 15.0
MIN_DELTA_CVD = 8.0
MIN_CONTRAST = 3.0


# Two adjacent filled bars that differ only in lightness read as the same
# colour, even when their total OKLab distance is large. Series identity has to
# live in hue and chroma, so the a/b plane is checked separately rather than
# letting a lightness gap carry the whole score.
MIN_DELTA_CHROMA = 9.0


def chroma_distance(hex_a: str, hex_b: str, kind: str | None = None) -> float:
    """Separation in the OKLab a/b plane only, ignoring lightness."""
    a, b = _hex_to_rgb(hex_a), _hex_to_rgb(hex_b)
    if kind:
        a, b = _simulate(a, kind), _simulate(b, kind)
    _, aa, ba = _to_oklab(a)
    _, ab, bb = _to_oklab(b)
    return 100.0 * math.dist((aa, ba), (ab, bb))


def separable(hex_a: str, hex_b: str) -> bool:
    if delta_e(hex_a, hex_b) < MIN_DELTA_NORMAL:
        return False
    if chroma_distance(hex_a, hex_b) < MIN_DELTA_CHROMA:
        return False
    return all(delta_e(hex_a, hex_b, kind) >= MIN_DELTA_CVD
               for kind in ("protan", "deutan", "tritan"))


# ---------------------------------------------------------------------------
# Country inks
# ---------------------------------------------------------------------------

# The strongest usable ink from each flag, adjusted where the literal flag hex
# would not hold 3:1 on white paper (yellows and light blues are darkened).
# `alt` gives a second option to try when the primaries of two teams collide.
COUNTRY_INK: dict[str, dict] = {
    "IRL": {"primary": "#16794a", "alt": "#d4610a"},   # green, orange
    "NED": {"primary": "#1a4f9c", "alt": "#c8102e"},   # blue, red
    "POR": {"primary": "#046a38", "alt": "#c8102e"},
    "MNE": {"primary": "#b32020", "alt": "#9a7a1a"},
    "SUI": {"primary": "#c8102e", "alt": "#6f6e6a"},
    "CRO": {"primary": "#c8102e", "alt": "#1a4f9c"},
    "CYP": {"primary": "#b4632a", "alt": "#4a7a2a"},   # copper, olive
    "MKD": {"primary": "#c8102e", "alt": "#a8801a"},
    "ISL": {"primary": "#1a4f9c", "alt": "#c8102e"},
    "UKR": {"primary": "#1a5fa8", "alt": "#9a7a05"},   # blue, darkened gold
    "LUX": {"primary": "#3a7fc4", "alt": "#c8102e"},
    "SWE": {"primary": "#1a5fa8", "alt": "#9a7a05"},
    "BIH": {"primary": "#1a3f8c", "alt": "#9a7a05"},
    "NOR": {"primary": "#1a3f7a", "alt": "#c8102e"},
    "BUL": {"primary": "#0a7a4a", "alt": "#c8102e"},
    "FIN": {"primary": "#1a5fa8", "alt": "#6f6e6a"},
    "AUT": {"primary": "#c8102e", "alt": "#6f6e6a"},
    "GBR": {"primary": "#1a3f8c", "alt": "#c8102e"},
    "EST": {"primary": "#2a6fbc", "alt": "#3d3d3b"},
    "HUN": {"primary": "#0a7a4a", "alt": "#c8102e"},
    "BEL": {"primary": "#9a7a05", "alt": "#c8102e"},
    "CZE": {"primary": "#1a3f8c", "alt": "#c8102e"},
    "DEN": {"primary": "#c8102e", "alt": "#6f6e6a"},
    "GER": {"primary": "#3d3d3b", "alt": "#c8102e"},
    "GRE": {"primary": "#1a5fa8", "alt": "#6f6e6a"},
    "ITA": {"primary": "#0a7a4a", "alt": "#c8102e"},
    "POL": {"primary": "#c8102e", "alt": "#6f6e6a"},
    "ROU": {"primary": "#1a3f8c", "alt": "#9a7a05"},
    "SVK": {"primary": "#1a3f8c", "alt": "#c8102e"},
    "SLO": {"primary": "#1a3f8c", "alt": "#c8102e"},
    "ESP": {"primary": "#c8102e", "alt": "#9a7a05"},
    "TUR": {"primary": "#c8102e", "alt": "#6f6e6a"},
    "LTU": {"primary": "#9a7a05", "alt": "#0a7a4a"},
    "LAT": {"primary": "#8a1a2a", "alt": "#6f6e6a"},
    "SRB": {"primary": "#1a3f8c", "alt": "#c8102e"},
    "GEO": {"primary": "#c8102e", "alt": "#6f6e6a"},
    "ALB": {"primary": "#b32020", "alt": "#3d3d3b"},
    "ISR": {"primary": "#1a5fa8", "alt": "#6f6e6a"},
    "MLT": {"primary": "#c8102e", "alt": "#6f6e6a"},
    "AZE": {"primary": "#1a8fa8", "alt": "#c8102e"},
    "ARM": {"primary": "#1a3f8c", "alt": "#d4610a"},
    "MDA": {"primary": "#1a3f8c", "alt": "#9a7a05"},
    "KOS": {"primary": "#1a3f8c", "alt": "#9a7a05"},
    "AND": {"primary": "#1a3f8c", "alt": "#c8102e"},
    "SMR": {"primary": "#3a7fc4", "alt": "#6f6e6a"},
    "GIB": {"primary": "#c8102e", "alt": "#3d3d3b"},
    "LIE": {"primary": "#1a3f8c", "alt": "#c8102e"},
    "MON": {"primary": "#c8102e", "alt": "#6f6e6a"},
}

# Used when a country is unknown, or when both of a team's own inks collide with
# the opponent. These are the validated default categorical hues.
FALLBACK_INKS = ["#2a78d6", "#eb6834", "#4a3aa7", "#1baf7a", "#e34948"]
NEUTRAL_INK = "#2a78d6"


def team_ink(code: str | None) -> str:
    entry = COUNTRY_INK.get((code or "").upper())
    return entry["primary"] if entry else NEUTRAL_INK


def pick_pair(code_a: str | None, code_b: str | None) -> tuple[str, str, bool]:
    """Colours for two teams: country identity where it survives the checks.

    Returns (ink_a, ink_b, b_substituted). The first team always keeps its
    country colour, because that is the team the report is about. The second
    tries its own primary, then its own alternate, then known-good hues.
    """
    a_entry = COUNTRY_INK.get((code_a or "").upper())
    b_entry = COUNTRY_INK.get((code_b or "").upper())

    ink_a = a_entry["primary"] if a_entry else FALLBACK_INKS[0]
    if contrast_ratio(ink_a) < MIN_CONTRAST and a_entry:
        ink_a = a_entry["alt"]

    # Try the opponent's own colours first, including lightness-shifted versions
    # of them, before giving up on their identity entirely.
    # A second authentic hue from the same flag beats a lightness shift of the
    # first, so both of the opponent's own colours are tried before either is
    # nudged. Portugal against Ireland becomes Portugal red, not a darker green.
    candidates: list[tuple[str, bool]] = []
    if b_entry:
        candidates.append((b_entry["primary"], True))
        candidates.append((b_entry["alt"], True))
        for base in (b_entry["primary"], b_entry["alt"]):
            for delta in (-0.10, 0.10, -0.18, 0.18):
                candidates.append((shift_lightness(base, delta), True))
    candidates += [(ink, False) for ink in FALLBACK_INKS]

    for candidate, is_own in candidates:
        if contrast_ratio(candidate) < MIN_CONTRAST:
            continue
        if separable(ink_a, candidate):
            return ink_a, candidate, not is_own

    # Nothing cleared: keep both identities rather than silently lie, and let the
    # caller know so the report can lean on labels instead of colour.
    return ink_a, (b_entry["primary"] if b_entry else FALLBACK_INKS[1]), True


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

# Compact specs. "h"/"v" are horizontal and vertical bands; "nordic" is an
# off-centre Scandinavian cross; "swiss" a centred cross; "wedge" a hoist
# triangle. Emblems are omitted: at badge size they are two or three pixels.
_FLAGS: dict[str, tuple] = {
    "IRL": ("v", ["#169b62", "#ffffff", "#ff883e"]),
    "NED": ("h", ["#ae1c28", "#ffffff", "#21468b"]),
    "POR": ("split", "#046a38", "#da291c", 0.4),
    "MNE": ("solid", "#c40308", "#d0aa4b"),
    "SUI": ("swiss", "#d52b1e", "#ffffff"),
    "CRO": ("h", ["#ff0000", "#ffffff", "#171796"]),
    "CYP": ("solid", "#ffffff", "#d57800"),
    "MKD": ("solid", "#d20000", "#f8e600"),
    "ISL": ("nordic", "#02529c", "#ffffff", "#dc1e35"),
    "UKR": ("h", ["#005bbb", "#ffd500"]),
    "LUX": ("h", ["#ed2939", "#ffffff", "#00a1de"]),
    "SWE": ("nordic", "#006aa7", "#fecc00", None),
    "BIH": ("solid", "#002395", "#fecb00"),
    "NOR": ("nordic", "#ba0c2f", "#ffffff", "#00205b"),
    "BUL": ("h", ["#ffffff", "#00966e", "#d62612"]),
    "FIN": ("nordic", "#ffffff", "#003580", None),
    "AUT": ("h", ["#ed2939", "#ffffff", "#ed2939"]),
    "GBR": ("union", None),
    "EST": ("h", ["#0072ce", "#000000", "#ffffff"]),
    "HUN": ("h", ["#cd2a3e", "#ffffff", "#436f4d"]),
    "BEL": ("v", ["#000000", "#fdda24", "#ef3340"]),
    "CZE": ("wedge", "#ffffff", "#d7141a", "#11457e"),
    "DEN": ("nordic", "#c8102e", "#ffffff", None),
    "GER": ("h", ["#000000", "#dd0000", "#ffce00"]),
    "GRE": ("h", ["#0d5eaf", "#ffffff", "#0d5eaf", "#ffffff", "#0d5eaf"]),
    "ITA": ("v", ["#008c45", "#ffffff", "#cd212a"]),
    "POL": ("h", ["#ffffff", "#dc143c"]),
    "ROU": ("v", ["#002b7f", "#fcd116", "#ce1126"]),
    "SVK": ("h", ["#ffffff", "#0b4ea2", "#ee1c25"]),
    "SLO": ("h", ["#ffffff", "#0000ff", "#ff0000"]),
    "ESP": ("h", ["#aa151b", "#f1bf00", "#f1bf00", "#aa151b"]),
    "TUR": ("solid", "#e30a17", "#ffffff"),
    "LTU": ("h", ["#fdb913", "#006a44", "#c1272d"]),
    "LAT": ("h", ["#9e3039", "#ffffff", "#9e3039"]),
    "SRB": ("h", ["#c6363c", "#0c4076", "#ffffff"]),
    "GEO": ("swiss", "#ffffff", "#ff0000"),
    "ALB": ("solid", "#e41e20", "#000000"),
    "ISR": ("h", ["#ffffff", "#0038b8", "#ffffff"]),
    "MLT": ("v", ["#ffffff", "#cf142b"]),
    "AZE": ("h", ["#00b5e2", "#ed2939", "#3f9c35"]),
    "ARM": ("h", ["#d90012", "#0033a0", "#f2a800"]),
    "MDA": ("v", ["#0033a0", "#ffd200", "#cc092f"]),
    "KOS": ("solid", "#244aa5", "#d0a650"),
    "AND": ("v", ["#10069f", "#fedd00", "#d50032"]),
    "SMR": ("h", ["#ffffff", "#5ec6e8"]),
    "GIB": ("h", ["#ffffff", "#ffffff", "#da000c"]),
    "LIE": ("h", ["#002b7f", "#ce1126"]),
    "MON": ("h", ["#ce1126", "#ffffff"]),
}


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def flag_svg(code: str | None, width: int = 16) -> str:
    """A small inline flag badge. Unknown codes get a neutral lettered chip."""
    code = (code or "").upper()
    height = round(width * 2 / 3)
    spec = _FLAGS.get(code)
    body = ""

    if spec is None:
        return (
            f'<svg class="flag" viewBox="0 0 3 2" width="{width}" height="{height}" '
            f'role="img" aria-label="{_esc(code)}"><rect width="3" height="2" '
            f'fill="#e8e8e4"/><rect width="3" height="2" fill="none" '
            f'stroke="rgba(0,0,0,.28)" stroke-width=".08"/></svg>'
        )

    kind = spec[0]
    if kind == "h":
        bands = spec[1]
        step = 2 / len(bands)
        body = "".join(
            f'<rect y="{i * step:.4f}" width="3" height="{step:.4f}" fill="{c}"/>'
            for i, c in enumerate(bands)
        )
    elif kind == "v":
        bands = spec[1]
        step = 3 / len(bands)
        body = "".join(
            f'<rect x="{i * step:.4f}" width="{step:.4f}" height="2" fill="{c}"/>'
            for i, c in enumerate(bands)
        )
    elif kind == "solid":
        field, mark = spec[1], spec[2]
        body = (f'<rect width="3" height="2" fill="{field}"/>'
                f'<circle cx="1.5" cy="1" r=".42" fill="{mark}"/>')
    elif kind == "split":
        left, right, ratio = spec[1], spec[2], spec[3]
        body = (f'<rect width="3" height="2" fill="{right}"/>'
                f'<rect width="{3 * ratio:.4f}" height="2" fill="{left}"/>')
    elif kind == "swiss":
        field, cross = spec[1], spec[2]
        body = (f'<rect width="3" height="2" fill="{field}"/>'
                f'<rect x="1.28" y=".3" width=".44" height="1.4" fill="{cross}"/>'
                f'<rect x=".8" y=".78" width="1.4" height=".44" fill="{cross}"/>')
    elif kind == "nordic":
        field, cross, inner = spec[1], spec[2], spec[3]
        body = f'<rect width="3" height="2" fill="{field}"/>'
        body += (f'<rect x=".78" width=".52" height="2" fill="{cross}"/>'
                 f'<rect y=".74" width="3" height=".52" fill="{cross}"/>')
        if inner:
            body += (f'<rect x=".9" width=".28" height="2" fill="{inner}"/>'
                     f'<rect y=".86" width="3" height=".28" fill="{inner}"/>')
    elif kind == "wedge":
        top, bottom, wedge = spec[1], spec[2], spec[3]
        body = (f'<rect width="3" height="1" fill="{top}"/>'
                f'<rect y="1" width="3" height="1" fill="{bottom}"/>'
                f'<path d="M0 0 L1.5 1 L0 2 Z" fill="{wedge}"/>')
    elif kind == "union":
        body = (
            '<rect width="3" height="2" fill="#012169"/>'
            '<path d="M0 0 L3 2 M3 0 L0 2" stroke="#ffffff" stroke-width=".44"/>'
            '<path d="M0 0 L3 2 M3 0 L0 2" stroke="#c8102e" stroke-width=".22"/>'
            '<path d="M1.5 0 V2 M0 1 H3" stroke="#ffffff" stroke-width=".72"/>'
            '<path d="M1.5 0 V2 M0 1 H3" stroke="#c8102e" stroke-width=".42"/>'
        )

    return (
        f'<svg class="flag" viewBox="0 0 3 2" width="{width}" height="{height}" '
        f'role="img" aria-label="{_esc(code)} flag">{body}'
        f'<rect width="3" height="2" fill="none" stroke="rgba(0,0,0,.28)" '
        f'stroke-width=".08"/></svg>'
    )
