"""Inline SVG charts.

Everything renders to a self-contained SVG string that references CSS custom
properties for colour, so light and dark mode swap in one place in the page
stylesheet. No JavaScript and no external assets, because these reports are
opened from a phone in a gym.

Native SVG <title> elements supply the hover layer. Every chart is paired with a
table in the templates, so no value is reachable only by hovering.
"""
from __future__ import annotations

import html
import math

from .config import BASKET_X, BASKET_Y, UNITS_PER_METRE
from .metrics import FOUR_FACTOR_HIGHER_IS_BETTER, FOUR_FACTOR_LABELS

# Court geometry in feed units, derived from the calibration in config.
_M = UNITS_PER_METRE
COURT_HALF_WIDTH = 7.5 * _M
COURT_LEFT = BASKET_X - COURT_HALF_WIDTH
COURT_RIGHT = BASKET_X + COURT_HALF_WIDTH
PAINT_HALF_WIDTH = 2.45 * _M
PAINT_DEPTH = 5.8 * _M
FT_CIRCLE_RADIUS = 1.8 * _M
THREE_RADIUS = 6.75 * _M
THREE_CORNER_X = 6.6 * _M
RIM_RADIUS = 0.225 * _M
PLOT_DEPTH = 11.0 * _M  # show a little beyond the arc, not the whole half court


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _fmt(value, digits=1, suffix="") -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


# --------------------------------------------------------------------------
# Shot chart
# --------------------------------------------------------------------------


def shot_chart(shots: list[dict], *, title: str = "", made_label: str = "Made",
               width: int = 300) -> str:
    """Half court with one marker per field goal attempt.

    Made and missed are separated by fill as well as hue, so the distinction
    never rests on colour alone.
    """
    plotted = [s for s in shots
               if s.get("x") is not None and s.get("y") is not None
               and not (s["x"] == 0 and s["y"] == 0)]  # free throws carry no location

    view_w = COURT_RIGHT - COURT_LEFT
    view_h = PLOT_DEPTH
    height = int(width * view_h / view_w)

    def sx(x):
        return x - COURT_LEFT

    def sy(y):
        return view_h - y  # basket sits at the bottom of the drawing

    lines = [
        f'<svg class="viz-shotchart" viewBox="0 0 {view_w:.0f} {view_h:.0f}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="{_esc(title or "Shot chart")}" '
        f'preserveAspectRatio="xMidYMid meet">',
        '<g class="court" fill="none" stroke="var(--court-line)" stroke-width="1.1">',
        f'<rect x="0.75" y="0.75" width="{view_w - 1.5:.1f}" '
        f'height="{view_h - 1.5:.1f}" />',
        # Paint.
        f'<rect x="{sx(BASKET_X - PAINT_HALF_WIDTH):.1f}" '
        f'y="{sy(PAINT_DEPTH):.1f}" width="{2 * PAINT_HALF_WIDTH:.1f}" '
        f'height="{PAINT_DEPTH:.1f}" />',
        # Free throw circle.
        f'<circle cx="{sx(BASKET_X):.1f}" cy="{sy(PAINT_DEPTH):.1f}" '
        f'r="{FT_CIRCLE_RADIUS:.1f}" />',
        # Rim and backboard.
        f'<circle cx="{sx(BASKET_X):.1f}" cy="{sy(BASKET_Y):.1f}" '
        f'r="{RIM_RADIUS:.1f}" />',
        f'<line x1="{sx(BASKET_X - 0.9 * _M):.1f}" y1="{sy(1.2 * _M):.1f}" '
        f'x2="{sx(BASKET_X + 0.9 * _M):.1f}" y2="{sy(1.2 * _M):.1f}" />',
    ]

    # Three point line: two straight corners joined by the arc.
    corner_y = BASKET_Y + math.sqrt(max(THREE_RADIUS**2 - THREE_CORNER_X**2, 0.0))
    sweep_start = (sx(BASKET_X - THREE_CORNER_X), sy(corner_y))
    sweep_end = (sx(BASKET_X + THREE_CORNER_X), sy(corner_y))
    # Sweep flag is 1, not 0: the drawing flips the y axis so the basket sits at
    # the bottom, which reverses the direction the arc has to bow.
    lines.append(
        f'<path d="M {sx(BASKET_X - THREE_CORNER_X):.1f} {sy(0):.1f} '
        f'L {sweep_start[0]:.1f} {sweep_start[1]:.1f} '
        f'A {THREE_RADIUS:.1f} {THREE_RADIUS:.1f} 0 0 1 '
        f'{sweep_end[0]:.1f} {sweep_end[1]:.1f} '
        f'L {sx(BASKET_X + THREE_CORNER_X):.1f} {sy(0):.1f}" />'
    )
    lines.append("</g>")

    # Markers. Missed sit underneath so makes stay legible in a dense cluster.
    # Shrink the marker as the plot fills up, otherwise a tournament-wide chart
    # is a solid mass of overlapping circles.
    count = len(plotted)
    if count <= 120:
        radius = 3.6
    elif count <= 280:
        radius = 3.0
    else:
        radius = 2.4
    for made_state in (False, True):
        group = [s for s in plotted if bool(s.get("made")) is made_state]
        if not group:
            continue
        css = "made" if made_state else "missed"
        lines.append(f'<g class="shot {css}">')
        for shot in group:
            label = shot.get("text") or ("made" if made_state else "missed")
            lines.append(
                f'<circle cx="{sx(shot["x"]):.1f}" cy="{sy(shot["y"]):.1f}" '
                f'r="{radius}"><title>{_esc(label)}</title></circle>'
            )
        lines.append("</g>")

    lines.append("</svg>")

    made_count = sum(1 for s in plotted if s.get("made"))
    legend = (
        '<div class="viz-legend">'
        f'<span class="key"><i class="swatch made"></i>{_esc(made_label)} '
        f'({made_count})</span>'
        f'<span class="key"><i class="swatch missed"></i>Missed '
        f'({len(plotted) - made_count})</span>'
        "</div>"
    )
    note = (
        '<p class="viz-note">Free throws carry no court location and are '
        "excluded.</p>"
    )
    return f'<figure class="viz">{"".join(lines)}{legend}{note}</figure>'


# --------------------------------------------------------------------------
# Four factors
# --------------------------------------------------------------------------


def four_factor_bars(team_factors: dict, opp_factors: dict, league: dict | None,
                     *, team_label: str, opp_label: str,
                     show_values: bool = False) -> str:
    """Grouped horizontal bars, one row per factor, with a league reference tick.

    Each factor has its own scale because they are not comparable to one
    another, so every row is drawn against its own maximum rather than forcing a
    shared axis that would flatten three of the four.

    In the reports this sits directly beneath the four factors table, so values
    are off by default: the table is where a reader reads a number, and printing
    the same eight figures twice on one page is noise.
    """
    rows = []
    row_h, bar_h, gap = 26, 8, 3
    label_w, value_w = 96, (52 if show_values else 8)
    width = 640
    track_w = width - label_w - value_w

    for key, label in FOUR_FACTOR_LABELS.items():
        team_value = team_factors.get(key)
        opp_value = opp_factors.get(key)
        league_value = (league or {}).get(key)
        scale_max = max([v for v in (team_value, opp_value, league_value, 1)
                         if v is not None]) * 1.15
        rows.append((key, label, team_value, opp_value, league_value, scale_max))

    height = row_h * len(rows) + 8
    out = [
        f'<svg class="viz-factors" viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" role="img" aria-label="Four factors comparison">'
    ]

    for index, (key, label, team_value, opp_value, league_value, scale_max) in \
            enumerate(rows):
        top = index * row_h + 4
        out.append(
            f'<text x="0" y="{top + 12}" class="axis-label">{_esc(label)}</text>'
        )

        for offset, (value, css, series_label) in enumerate((
            (team_value, "series-1", team_label),
            (opp_value, "series-2", opp_label),
        )):
            y = top + offset * (bar_h + gap)
            out.append(
                f'<rect x="{label_w}" y="{y}" width="{track_w}" height="{bar_h}" '
                f'rx="1" class="track" />'
            )
            if value is not None:
                bar_w = max(2.0, min(track_w, track_w * value / scale_max))
                out.append(
                    f'<rect x="{label_w}" y="{y}" width="{bar_w:.1f}" '
                    f'height="{bar_h}" rx="1" class="bar {css}">'
                    f"<title>{_esc(series_label)} {_esc(label)} "
                    f"{_fmt(value)}</title></rect>"
                )
                if show_values:
                    out.append(
                        f'<text x="{label_w + track_w + 5}" y="{y + bar_h - 0.5}" '
                        f'class="value">{_fmt(value)}</text>'
                    )

        if league_value is not None:
            tick_x = label_w + min(track_w, track_w * league_value / scale_max)
            out.append(
                f'<line x1="{tick_x:.1f}" y1="{top - 2}" x2="{tick_x:.1f}" '
                f'y2="{top + 2 * bar_h + gap + 2}" class="reference">'
                f"<title>Event average {_fmt(league_value)}</title></line>"
            )

    out.append("</svg>")

    legend = (
        '<div class="viz-legend">'
        f'<span class="key"><i class="swatch series-1"></i>{_esc(team_label)}</span>'
        f'<span class="key"><i class="swatch series-2"></i>{_esc(opp_label)}</span>'
        '<span class="key"><i class="swatch reference"></i>Event average</span>'
        "</div>"
    )
    return f'<figure class="viz">{"".join(out)}{legend}</figure>'


# --------------------------------------------------------------------------
# Margin timeline
# --------------------------------------------------------------------------


def margin_timeline(points: list[dict], *, team_label: str,
                    period_seconds: int = 600, periods: int = 4) -> str:
    """Score margin over the game, filled above and below a neutral zero line.

    Ahead and behind are opposite states, so this is a diverging encoding: two
    hues that read as opposite with a neutral midpoint, not one colour ramp.
    """
    if not points:
        return ""

    width, height = 640, 122
    pad_l, pad_r, pad_t, pad_b = 26, 6, 10, 18
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    total_seconds = max(p["seconds"] for p in points) or period_seconds * periods

    # Scale to the margins actually reached rather than forcing symmetry, so a
    # team that never trailed does not spend half the chart on empty space.
    # Zero always stays on the axis, so "ahead or behind" is never ambiguous.
    margins = [p["margin"] for p in points]
    top = int(math.ceil(max(max(margins), 0) / 5.0) * 5)
    bottom = int(math.floor(min(min(margins), 0) / 5.0) * 5)
    if top == bottom:  # a wire-to-wire tie, vanishingly rare but not impossible
        top, bottom = 5, -5
    span = top - bottom

    def px(seconds):
        return pad_l + plot_w * seconds / total_seconds

    def py(margin):
        return pad_t + plot_h * (top - margin) / span

    ordered = sorted(points, key=lambda p: p["seconds"])
    # Step path: the margin holds until the next scoring event.
    coords = []
    previous = None
    for point in ordered:
        if previous is not None:
            coords.append((px(point["seconds"]), py(previous)))
        coords.append((px(point["seconds"]), py(point["margin"])))
        previous = point["margin"]

    zero_y = py(0)
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area_ahead = (f'M {pad_l},{zero_y:.1f} L ' + path
                  + f' L {coords[-1][0]:.1f},{zero_y:.1f} Z') if coords else ""

    out = [
        f'<svg class="viz-timeline" viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" role="img" '
        f'aria-label="{_esc(team_label)} score margin over the game">',
        f'<defs>'
        f'<clipPath id="clip-ahead"><rect x="0" y="{pad_t}" width="{width}" '
        f'height="{zero_y - pad_t:.1f}"/></clipPath>'
        f'<clipPath id="clip-behind"><rect x="0" y="{zero_y:.1f}" width="{width}" '
        f'height="{pad_t + plot_h - zero_y:.1f}"/></clipPath>'
        f"</defs>",
    ]

    for tick in sorted({top, 0, bottom}, reverse=True):
        y = py(tick)
        css = "zero" if tick == 0 else "grid"
        out.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'class="{css}" />'
        )
        label = "0" if tick == 0 else f"{tick:+d}"
        out.append(
            f'<text x="{pad_l - 5}" y="{y + 3.5:.1f}" class="axis-tick" '
            f'text-anchor="end">{label}</text>'
        )

    # Period separators, labelled in the middle of each period they open.
    for period in range(periods):
        start_x = px(period * period_seconds)
        end_x = px(min((period + 1) * period_seconds, total_seconds))
        if period:
            out.append(f'<line x1="{start_x:.1f}" y1="{pad_t}" x2="{start_x:.1f}" '
                       f'y2="{pad_t + plot_h}" class="grid" />')
        if end_x > start_x:
            out.append(
                f'<text x="{(start_x + end_x) / 2:.1f}" y="{height - 6}" '
                f'class="axis-tick" text-anchor="middle">Q{period + 1}</text>'
            )

    if area_ahead:
        out.append(f'<path d="{area_ahead}" class="fill-ahead" '
                   f'clip-path="url(#clip-ahead)" />')
        out.append(f'<path d="{area_ahead}" class="fill-behind" '
                   f'clip-path="url(#clip-behind)" />')
    out.append(f'<polyline points="{path}" class="line" />')

    final = ordered[-1]["margin"]
    out.append(
        f'<text x="{width - pad_r}" y="{py(final) - 6:.1f}" class="value" '
        f'text-anchor="end">{final:+d}</text>'
    )
    out.append("</svg>")

    legend = (
        '<div class="viz-legend">'
        f'<span class="key"><i class="swatch fill-ahead"></i>'
        f'{_esc(team_label)} ahead</span>'
        f'<span class="key"><i class="swatch fill-behind"></i>'
        f'{_esc(team_label)} behind</span>'
        "</div>"
    )
    return f'<figure class="viz">{"".join(out)}{legend}</figure>'


# --------------------------------------------------------------------------
# Shot zones
# --------------------------------------------------------------------------


def zone_bars(zones: list[dict], *, metric: str = "points_per_attempt") -> str:
    """One series of bars: efficiency by zone.

    Points per attempt rather than field goal percentage, because that is the
    only way a three and a layup compare honestly.
    """
    active = [z for z in zones if z["attempts"] > 0]
    if not active:
        return '<p class="viz-note">No field goal attempts recorded.</p>'

    width = 640
    row_h, bar_h = 19, 8
    label_w, value_w = 84, 104
    track_w = width - label_w - value_w
    scale_max = max(1.5, max(z[metric] or 0 for z in active) * 1.1)
    height = row_h * len(active) + 6

    out = [
        f'<svg class="viz-zones" viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" role="img" aria-label="Efficiency by shot zone">'
    ]
    for index, zone in enumerate(active):
        y = index * row_h + 6
        value = zone[metric] or 0
        out.append(f'<text x="0" y="{y + bar_h - 0.5}" class="axis-label">'
                   f'{_esc(zone["zone"])}</text>')
        out.append(f'<rect x="{label_w}" y="{y}" width="{track_w}" '
                   f'height="{bar_h}" rx="1" class="track" />')
        bar_w = max(2.0, min(track_w, track_w * value / scale_max))
        out.append(
            f'<rect x="{label_w}" y="{y}" width="{bar_w:.1f}" height="{bar_h}" '
            f'rx="1" class="bar series-1">'
            f'<title>{_esc(zone["zone"])}: {zone["makes"]}/{zone["attempts"]}, '
            f'{_fmt(value, 2)} points per attempt</title></rect>'
        )
        out.append(
            f'<text x="{label_w + track_w + 6}" y="{y + bar_h - 2}" class="value">'
            f'{_fmt(value, 2)} · {zone["makes"]}/{zone["attempts"]}</text>'
        )
    out.append("</svg>")
    return (f'<figure class="viz">{"".join(out)}'
            f'<p class="viz-note">Bar length is points per attempt.</p></figure>')
