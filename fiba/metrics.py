"""Derived basketball metrics: four factors, ratings, player advanced, shot zones."""
from __future__ import annotations

import math

from .config import BASKET_X, BASKET_Y, UNITS_PER_METRE

# Share of free throw attempts that end a possession. The standard 0.44 is
# retained so figures stay comparable to published basketball analytics.
FT_POSSESSION_WEIGHT = 0.44


def _safe_div(numerator, denominator):
    if not denominator:
        return None
    return numerator / denominator


def _pct(numerator, denominator):
    value = _safe_div(numerator, denominator)
    return None if value is None else 100.0 * value


def possessions(team: dict, opponent: dict) -> float:
    """Averaged with the opponent, which is the usual way to steady the estimate."""
    def one(t):
        return ((t.get("FGA") or 0)
                + FT_POSSESSION_WEIGHT * (t.get("FTA") or 0)
                - (t.get("OR") or 0)
                + (t.get("TO") or 0))
    return 0.5 * (one(team) + one(opponent))


def four_factors(team: dict, opponent: dict) -> dict:
    """The four factors, plus the pace and rating context they need to be read."""
    fga = team.get("FGA") or 0
    fgm = team.get("FGM") or 0
    fg3m = team.get("FG3M") or 0
    fta = team.get("FTA") or 0
    ftm = team.get("FTM") or 0
    poss = possessions(team, opponent)

    return {
        "efg_pct": _pct(fgm + 0.5 * fg3m, fga),
        "tov_pct": _pct(team.get("TO") or 0, poss),
        "oreb_pct": _pct(team.get("OR") or 0,
                         (team.get("OR") or 0) + (opponent.get("DR") or 0)),
        # The defensive twin of OREB%: the share of available defensive boards
        # this team secured. Reported alongside so a defensive four factors
        # table reads in the same direction as the offensive one.
        "dreb_pct": _pct(team.get("DR") or 0,
                         (team.get("DR") or 0) + (opponent.get("OR") or 0)),
        "ft_rate": _pct(fta, fga),          # attempts per field goal attempt
        "ft_made_rate": _pct(ftm, fga),     # the made-shot variant, shown alongside
        "possessions": poss,
        "off_rating": _pct(team.get("PTS") or 0, poss),   # points per 100
        "def_rating": _pct(opponent.get("PTS") or 0, poss),
        "net_rating": (_pct(team.get("PTS") or 0, poss) or 0)
                      - (_pct(opponent.get("PTS") or 0, poss) or 0),
        "ts_pct": _pct(team.get("PTS") or 0,
                       2 * (fga + FT_POSSESSION_WEIGHT * fta)),
        "fg3_rate": _pct(team.get("FG3A") or 0, fga),  # share of shots from three
    }


DEFENSIVE_FOUR_FACTOR_LABELS = {
    "efg_pct": "Opp eFG%",
    "tov_pct": "TOV% forced",
    "dreb_pct": "DREB%",
    "ft_rate": "Opp FTA/FGA",
}

# Read from the defending team's side: forcing turnovers and grabbing defensive
# boards are good; letting the opponent shoot well or reach the line is bad.
DEFENSIVE_HIGHER_IS_BETTER = {
    "efg_pct": False,
    "tov_pct": True,
    "dreb_pct": True,
    "ft_rate": False,
}

FOUR_FACTOR_LABELS = {
    "efg_pct": "eFG%",
    "tov_pct": "TOV%",
    "oreb_pct": "OREB%",
    "ft_rate": "FTA/FGA",
}

# Whether a higher value is better, used to colour the report tables.
FOUR_FACTOR_HIGHER_IS_BETTER = {
    "efg_pct": True,
    "tov_pct": False,
    "oreb_pct": True,
    "ft_rate": True,
}


def player_advanced(player: dict, team: dict, opponent: dict,
                    team_seconds: int) -> dict:
    """Rate stats for one player.

    `team_seconds` is total team player-seconds (five players times game length),
    so the on-court share is player_seconds / (team_seconds / 5).
    """
    seconds = player.get("seconds_played") or 0
    if seconds <= 0:
        return {k: None for k in (
            "usage_pct", "ts_pct", "efg_pct", "ast_pct", "oreb_pct", "dreb_pct",
            "treb_pct", "stl_pct", "blk_pct", "tov_pct", "pts_per40", "reb_per40",
            "ast_per40", "minutes")}

    share_denominator = team_seconds / 5.0
    fga = player.get("FGA") or 0
    fta = player.get("FTA") or 0
    turnovers = player.get("TO") or 0
    plays = fga + FT_POSSESSION_WEIGHT * fta + turnovers

    team_plays = ((team.get("FGA") or 0)
                  + FT_POSSESSION_WEIGHT * (team.get("FTA") or 0)
                  + (team.get("TO") or 0))
    opp_poss = possessions(opponent, team)

    team_fgm = team.get("FGM") or 0
    on_court_share = _safe_div(seconds, share_denominator) or 0
    teammate_fgm = on_court_share * team_fgm - (player.get("FGM") or 0)

    def rate_per_40(value):
        return _safe_div((value or 0) * 2400.0, seconds)

    total_reb = (team.get("REB") or 0) + (opponent.get("REB") or 0)

    return {
        "minutes": seconds / 60.0,
        "usage_pct": _pct(plays * share_denominator, seconds * team_plays),
        "ts_pct": _pct(player.get("PTS") or 0,
                       2 * (fga + FT_POSSESSION_WEIGHT * fta)),
        "efg_pct": _pct((player.get("FGM") or 0) + 0.5 * (player.get("FG3M") or 0), fga),
        "ast_pct": _pct(player.get("AS") or 0, teammate_fgm) if teammate_fgm > 0 else None,
        "oreb_pct": _pct((player.get("OR") or 0) * share_denominator,
                         seconds * ((team.get("OR") or 0) + (opponent.get("DR") or 0))),
        "dreb_pct": _pct((player.get("DR") or 0) * share_denominator,
                         seconds * ((team.get("DR") or 0) + (opponent.get("OR") or 0))),
        "treb_pct": _pct((player.get("REB") or 0) * share_denominator,
                         seconds * total_reb),
        "stl_pct": _pct((player.get("ST") or 0) * share_denominator,
                        seconds * opp_poss),
        "blk_pct": _pct((player.get("BS") or 0) * share_denominator,
                        seconds * (opponent.get("FG2A") or 0)),
        "tov_pct": _pct(turnovers, plays),
        "pts_per40": rate_per_40(player.get("PTS")),
        "reb_per40": rate_per_40(player.get("REB")),
        "ast_per40": rate_per_40(player.get("AS")),
    }


# --------------------------------------------------------------------------
# Shot location
# --------------------------------------------------------------------------

# FIBA court measurements, in metres.
PAINT_HALF_WIDTH_M = 2.45
PAINT_DEPTH_M = 5.8
RIM_RADIUS_M = 1.6

ZONE_ORDER = ["Rim", "Paint", "Mid-range", "Corner 3", "Wing 3", "Top 3"]


def shot_geometry(x: float | None, y: float | None) -> dict | None:
    """Convert feed coordinates into metres relative to the basket.

    Free throws are logged at (0, 0) and carry no location, so they are excluded
    by the caller rather than mapped to a spot on the floor.
    """
    if x is None or y is None:
        return None
    dx = (x - BASKET_X) / UNITS_PER_METRE
    dy = (y - BASKET_Y) / UNITS_PER_METRE
    distance = math.hypot(dx, dy)
    # 0 degrees points along the baseline, 90 degrees straight out from the rim.
    angle = math.degrees(math.atan2(dy, abs(dx))) if (dx or dy) else 90.0
    return {"dx_m": dx, "dy_m": dy, "distance_m": distance, "angle_deg": angle,
            "baseline_m": y / UNITS_PER_METRE}


def classify_zone(event: dict) -> str | None:
    """Bucket a field goal attempt into a shooting zone.

    The feed's own text already names a zone ("under the basket", "inside
    paint", "3pt jump shot from center"), so geometry and text are combined:
    text decides two versus three, geometry decides where.
    """
    code = event.get("action_code")
    if code not in ("P2", "P3"):
        return None

    geometry = shot_geometry(event.get("x"), event.get("y"))
    if geometry is None:
        return None

    if code == "P3":
        angle = geometry["angle_deg"]
        if angle < 20:
            return "Corner 3"
        if angle < 60:
            return "Wing 3"
        return "Top 3"

    text = (event.get("text") or "").lower()
    if "under the basket" in text or geometry["distance_m"] <= RIM_RADIUS_M:
        return "Rim"
    in_paint = (abs(geometry["dx_m"]) <= PAINT_HALF_WIDTH_M
                and geometry["baseline_m"] <= PAINT_DEPTH_M)
    if in_paint or "inside paint" in text:
        return "Paint"
    return "Mid-range"


def zone_breakdown(events: list[dict], org_id: int | None = None) -> list[dict]:
    """Attempts, makes and efficiency per zone."""
    buckets: dict[str, dict] = {
        zone: {"zone": zone, "attempts": 0, "makes": 0, "points": 0}
        for zone in ZONE_ORDER
    }
    for event in events:
        if org_id is not None and event.get("org_id") != org_id:
            continue
        zone = classify_zone(event)
        if zone is None:
            continue
        bucket = buckets[zone]
        bucket["attempts"] += 1
        if event.get("made"):
            bucket["makes"] += 1
            bucket["points"] += event.get("shot_points") or 0

    total_attempts = sum(b["attempts"] for b in buckets.values())
    out = []
    for zone in ZONE_ORDER:
        bucket = buckets[zone]
        bucket["fg_pct"] = _pct(bucket["makes"], bucket["attempts"])
        bucket["share_pct"] = _pct(bucket["attempts"], total_attempts)
        # Points per attempt is the honest way to compare a three to a layup.
        bucket["points_per_attempt"] = _safe_div(bucket["points"], bucket["attempts"])
        out.append(bucket)
    return out


def zone_text_agreement(events: list[dict]) -> dict:
    """Cross-check geometric zoning against the feed's own wording.

    Disagreements are a signal that the coordinate calibration has drifted, so
    they are surfaced rather than silently absorbed.
    """
    checked = mismatched = 0
    examples = []
    for event in events:
        if event.get("action_code") != "P2":
            continue
        text = (event.get("text") or "").lower()
        if "under the basket" not in text:
            continue
        geometry = shot_geometry(event.get("x"), event.get("y"))
        if geometry is None:
            continue
        checked += 1
        if geometry["distance_m"] > RIM_RADIUS_M + 0.75:
            mismatched += 1
            if len(examples) < 5:
                examples.append(
                    {"x": event.get("x"), "y": event.get("y"),
                     "distance_m": round(geometry["distance_m"], 2),
                     "text": event.get("text")}
                )
    return {
        "checked": checked,
        "mismatched": mismatched,
        "mismatch_pct": _pct(mismatched, checked),
        "examples": examples,
    }
