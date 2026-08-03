"""Assemble report-ready structures from the database.

Everything here works on a team plus a set of games, so the same functions serve
a single-game review and a whole-tournament scouting profile.
"""
from __future__ import annotations

from collections import defaultdict

from . import metrics
from .config import PERIOD_SECONDS
from .parse import _PLAYER_STAT_KEYS, _TEAM_STAT_KEYS

# Below this much shared court time, a per-100 rate says more about rounding than
# about the players. The underlying minutes and raw plus-minus are still shown,
# because those are facts; only the extrapolated rate is withheld.
MIN_RATE_SECONDS = 180


def _sum_rows(rows, keys) -> dict:
    total = {k: 0 for k in keys}
    for row in rows:
        for key in keys:
            value = row[key] if key in row.keys() else None
            if value is not None:
                total[key] += value
    return total


def event_teams(conn, event_slug: str) -> list[dict]:
    """Every team in the event with a games-played count."""
    cur = conn.execute(
        """
        SELECT t.org_id,
               COALESCE(ga.team_a_code, gb.team_b_code) AS code,
               COALESCE(ga.team_a_name, gb.team_b_name) AS name,
               COUNT(*) AS games
        FROM team_game_stats t
        JOIN games g ON g.event_slug=t.event_slug AND g.game_id=t.game_id
        LEFT JOIN games ga ON ga.event_slug=t.event_slug AND ga.game_id=t.game_id
                          AND ga.team_a_org_id=t.org_id
        LEFT JOIN games gb ON gb.event_slug=t.event_slug AND gb.game_id=t.game_id
                          AND gb.team_b_org_id=t.org_id
        WHERE t.event_slug=?
        GROUP BY t.org_id
        ORDER BY name
        """,
        (event_slug,),
    )
    return [dict(r) for r in cur]


def team_identity(conn, event_slug: str, org_id: int) -> dict:
    row = conn.execute(
        """
        SELECT CASE WHEN team_a_org_id=? THEN team_a_code ELSE team_b_code END AS code,
               CASE WHEN team_a_org_id=? THEN team_a_name ELSE team_b_name END AS name
        FROM games
        WHERE event_slug=? AND (team_a_org_id=? OR team_b_org_id=?)
        LIMIT 1
        """,
        (org_id, org_id, event_slug, org_id, org_id),
    ).fetchone()
    if row is None:
        return {"org_id": org_id, "code": str(org_id), "name": str(org_id)}
    return {"org_id": org_id, "code": row["code"], "name": row["name"]}


def team_games(conn, event_slug: str, org_id: int) -> list[dict]:
    """One row per game this team has played, own line plus opponent line."""
    cur = conn.execute(
        """
        SELECT t.*, g.game_datetime, g.game_utc, g.status_code, g.lineups_ok,
               g.team_a_org_id, g.team_a_code, g.team_b_code
        FROM team_game_stats t
        JOIN games g ON g.event_slug=t.event_slug AND g.game_id=t.game_id
        WHERE t.event_slug=? AND t.org_id=?
        ORDER BY g.game_utc, t.game_id
        """,
        (event_slug, org_id),
    )
    games = []
    for row in cur:
        opponent = conn.execute(
            "SELECT * FROM team_game_stats WHERE event_slug=? AND game_id=? AND org_id=?",
            (event_slug, row["game_id"], row["opp_org_id"]),
        ).fetchone()
        if opponent is None:
            continue
        entry = dict(row)
        entry["opponent"] = dict(opponent)
        entry["opponent_identity"] = team_identity(conn, event_slug, row["opp_org_id"])
        entry["four_factors"] = metrics.four_factors(dict(row), dict(opponent))
        entry["opp_four_factors"] = metrics.four_factors(dict(opponent), dict(row))
        games.append(entry)
    return games


def team_profile(conn, event_slug: str, org_id: int,
                 game_ids: list[int] | None = None) -> dict:
    """Aggregate a team across some or all of its games."""
    games = team_games(conn, event_slug, org_id)
    if game_ids is not None:
        wanted = set(game_ids)
        games = [g for g in games if g["game_id"] in wanted]

    own = _sum_rows(games, _TEAM_STAT_KEYS)
    opp = _sum_rows([g["opponent"] for g in games], _TEAM_STAT_KEYS)

    played = len(games)
    wins = sum(1 for g in games if g["won"])
    points_for = sum(g["score"] or 0 for g in games)
    points_against = sum(g["opp_score"] or 0 for g in games)

    factors = metrics.four_factors(own, opp) if played else {}
    opp_factors = metrics.four_factors(opp, own) if played else {}

    return {
        "identity": team_identity(conn, event_slug, org_id),
        "games_played": played,
        "wins": wins,
        "losses": played - wins,
        "game_ids": [g["game_id"] for g in games],
        "games": games,
        "totals": own,
        "opp_totals": opp,
        "four_factors": factors,
        "opp_four_factors": opp_factors,
        "points_for_pg": points_for / played if played else None,
        "points_against_pg": points_against / played if played else None,
        "pace": (factors.get("possessions") / played) if played and factors else None,
        # FIBA supplies these directly rather than us deriving them.
        "pip_pg": own["A_PIP"] / played if played else None,
        "fastbreak_pg": own["A_FBP"] / played if played else None,
        "second_chance_pg": own["A_SCP"] / played if played else None,
        "points_off_to_pg": own["A_PAT"] / played if played else None,
        "bench_points_pg": own["A_PFB"] / played if played else None,
    }


# Keys every caller may read off an event-average dict, so templates can render
# before a single game has been played.
_LEAGUE_KEYS = (
    "efg_pct", "tov_pct", "oreb_pct", "dreb_pct", "ft_rate", "ft_made_rate",
    "possessions",
    "off_rating", "def_rating", "net_rating", "ts_pct", "fg3_rate", "pace",
    "points_pg",
)


def event_averages(conn, event_slug: str) -> dict:
    """Competition-wide baselines, so a team's numbers have something to sit against.

    Before the first game there is nothing to average, but the shape must stay
    the same so reports still render on the morning of the opener.
    """
    rows = conn.execute(
        "SELECT * FROM team_game_stats WHERE event_slug=?", (event_slug,)
    ).fetchall()
    if not rows:
        return dict.fromkeys(_LEAGUE_KEYS)
    total = _sum_rows(rows, _TEAM_STAT_KEYS)
    # Every game contributes both teams, so the league is its own opponent.
    factors = metrics.four_factors(total, total)
    team_games_count = len(rows)
    factors["pace"] = factors["possessions"] / team_games_count if team_games_count else None
    factors["points_pg"] = total["PTS"] / team_games_count if team_games_count else None
    return factors


# Everything a team can be ranked on, and whether a bigger number is better.
# Grouped the way the leaderboard presents them. Pace and the shot-share metrics
# have no better/worse direction, so they carry None and are ranked but never
# shaded: taking a lot of threes is a style, not an achievement.
TEAM_METRICS: dict[str, dict] = {
    # Advanced, own
    "pace": {"group": "advanced", "better": None, "label": "Pace", "dp": 0},
    "off_rating": {"group": "advanced", "better": True, "label": "ORtg"},
    "def_rating": {"group": "advanced", "better": False, "label": "DRtg"},
    "net_rating": {"group": "advanced", "better": True, "label": "Net/100", "signed": True},
    "efg_pct": {"group": "advanced", "better": True, "label": "eFG%"},
    "tov_pct": {"group": "advanced", "better": False, "label": "TOV%"},
    "oreb_pct": {"group": "advanced", "better": True, "label": "OREB%"},
    "ft_rate": {"group": "advanced", "better": True, "label": "FTA/FGA"},
    # Advanced, allowed
    "opp_efg_pct": {"group": "advanced", "better": False, "label": "Opp eFG%"},
    "tov_forced_pct": {"group": "advanced", "better": True, "label": "TOV% frc"},
    "dreb_pct": {"group": "advanced", "better": True, "label": "DREB%"},
    "opp_ft_rate": {"group": "advanced", "better": False, "label": "Opp FTr"},
    # Box score, per game
    "pts": {"group": "box", "better": True, "label": "PTS"},
    "fg_pct": {"group": "box", "better": True, "label": "FG%"},
    "fg3_pct": {"group": "box", "better": True, "label": "3P%"},
    "ft_pct": {"group": "box", "better": True, "label": "FT%"},
    "oreb": {"group": "box", "better": True, "label": "OR"},
    "dreb": {"group": "box", "better": True, "label": "DR"},
    "ast": {"group": "box", "better": True, "label": "AST"},
    "tov": {"group": "box", "better": False, "label": "TO"},
    "stl": {"group": "box", "better": True, "label": "STL"},
    "blk": {"group": "box", "better": True, "label": "BLK"},
    "pf": {"group": "box", "better": False, "label": "PF"},
    # The same box score from the other side: what this team allows. Directions
    # flip with meaning rather than with the sign, so opponent turnovers are
    # turnovers forced (good) while opponent steals are our own giveaways (bad).
    # Opponent free throw percentage is ranked but never shaded: nobody contests
    # a free throw, so shading it would grade a team on something it cannot
    # affect.
    "opp_pts": {"group": "box", "better": False, "label": "Opp PTS"},
    "opp_fg_pct": {"group": "box", "better": False, "label": "Opp FG%"},
    "opp_fg3_pct": {"group": "box", "better": False, "label": "Opp 3P%"},
    "opp_ft_pct": {"group": "box", "better": None, "label": "Opp FT%"},
    "opp_oreb": {"group": "box", "better": False, "label": "Opp OR"},
    "opp_dreb": {"group": "box", "better": False, "label": "Opp DR"},
    "opp_ast": {"group": "box", "better": False, "label": "Opp AST"},
    "opp_tov": {"group": "box", "better": True, "label": "TO forced"},
    "opp_stl": {"group": "box", "better": False, "label": "Opp STL"},
    "opp_blk": {"group": "box", "better": False, "label": "Opp BLK"},
    "opp_pf": {"group": "box", "better": True, "label": "Fouls drawn"},
    # Shot profile. 3PT rate is FG3A/FGA off the box score, so it is ranked but
    # never shaded and never reaches the leaderboard, where the zone-derived
    # three-point share already covers the same ground.
    "fg3_rate": {"group": "advanced", "better": None, "label": "3PT rate"},
    # Shooting by zone, own then allowed. No shot volume is shaded: where a team
    # chooses to shoot from is a style, and the accuracy and points-per-attempt
    # columns sitting beside it already say whether the choice is working. Shares
    # still carry a rank, which is the part that answers "do they take more of
    # these than anyone else".
    "rim_share": {"group": "shot", "better": None, "label": "Rim %sh"},
    "mid_range_share": {"group": "shot", "better": None, "label": "Mid %sh"},
    "mid_range_fg": {"group": "shot", "better": True, "label": "Mid FG%"},
    "three_share": {"group": "shot", "better": None, "label": "3PT %sh"},
    "three_fg": {"group": "shot", "better": True, "label": "3PT FG%"},
    "opp_rim_share": {"group": "shot", "better": None, "label": "Opp Rim %sh"},
    "opp_mid_range_share": {"group": "shot", "better": None, "label": "Opp Mid %sh"},
    "opp_mid_range_fg": {"group": "shot", "better": False, "label": "Opp Mid FG%"},
    "opp_three_share": {"group": "shot", "better": None, "label": "Opp 3PT %sh"},
    "opp_three_fg": {"group": "shot", "better": False, "label": "Opp 3PT FG%"},
    # Scoring breakdown, per game
    "pip": {"group": "scoring", "better": True, "label": "Paint"},
    "fbp": {"group": "scoring", "better": True, "label": "Fast break"},
    "scp": {"group": "scoring", "better": True, "label": "2nd chance"},
    "pat": {"group": "scoring", "better": True, "label": "Off TO"},
    "bench": {"group": "scoring", "better": True, "label": "Bench"},
    "opp_pip": {"group": "scoring", "better": False, "label": "Opp paint"},
    "opp_fbp": {"group": "scoring", "better": False, "label": "Opp fast br"},
    "opp_scp": {"group": "scoring", "better": False, "label": "Opp 2nd ch"},
    "opp_pat": {"group": "scoring", "better": False, "label": "Opp off TO"},
}


def zone_metric(zone: str, stat: str, prefix: str = "") -> str:
    """Metric key for one zone of the shot chart, e.g. ``opp_corner_3_fg``.

    `metrics.zone_breakdown` stamps the same slugs onto every row it emits, so a
    zone table can look its own ranks up without the template knowing the naming
    rule.
    """
    return f"{prefix}{zone.lower().replace(' ', '_').replace('-', '_')}_{stat}"


# Every zone is rankable on both volume and accuracy, on both sides of the ball,
# so the shot-zone tables can shade every cell. The leaderboard shows only the
# curated handful declared above; these fill in the rest. setdefault, so a
# hand-written entry above always wins over the generated default.
for _zone in metrics.ZONE_ORDER:
    _label = "Mid" if _zone == "Mid-range" else _zone
    TEAM_METRICS.setdefault(zone_metric(_zone, "share"), {
        "group": "shot", "better": None, "label": f"{_label} %sh"})
    TEAM_METRICS.setdefault(zone_metric(_zone, "fg"), {
        "group": "shot", "better": True, "label": f"{_label} FG%"})
    # Points per attempt needs no judgement call: scoring more per shot from a
    # spot is good wherever the spot is, and conceding more is bad.
    TEAM_METRICS.setdefault(zone_metric(_zone, "ppa"), {
        "group": "shot", "better": True, "label": f"{_label} pts/att", "dp": 2})
    TEAM_METRICS.setdefault(zone_metric(_zone, "share", "opp_"), {
        "group": "shot", "better": None, "label": f"Opp {_label} %sh"})
    TEAM_METRICS.setdefault(zone_metric(_zone, "fg", "opp_"), {
        "group": "shot", "better": False, "label": f"Opp {_label} FG%"})
    TEAM_METRICS.setdefault(zone_metric(_zone, "ppa", "opp_"), {
        "group": "shot", "better": False, "label": f"Opp {_label} pts/att", "dp": 2})
del _zone, _label


# How the leaderboard groups its columns. Each view is a readable width on its
# own; showing all forty-odd at once would not be.
LEADERBOARD_GROUPS = [
    {"key": "adv", "label": "Advanced", "metrics": [
        "pace", "off_rating", "def_rating", "net_rating",
        "efg_pct", "tov_pct", "oreb_pct", "ft_rate",
        "opp_efg_pct", "tov_forced_pct", "dreb_pct", "opp_ft_rate"]},
    {"key": "box", "label": "Box score", "metrics": [
        "pts", "fg_pct", "fg3_pct", "ft_pct", "oreb", "dreb", "ast", "tov",
        "stl", "blk", "pf",
        "opp_pts", "opp_fg_pct", "opp_fg3_pct", "opp_ft_pct", "opp_oreb",
        "opp_dreb", "opp_ast", "opp_tov", "opp_stl", "opp_blk", "opp_pf"]},
    # Volume then accuracy for each zone, own block then conceded, so the two
    # halves line up column for column. The three-point zones stay aggregated
    # here: splitting corner, wing and top is what the zone tables on a scouting
    # page are for, and six more columns would not survive the width.
    {"key": "shot", "label": "Shooting", "metrics": [
        "rim_share", "rim_fg", "paint_share", "paint_fg",
        "mid_range_share", "mid_range_fg", "three_share", "three_fg",
        "opp_rim_share", "opp_rim_fg", "opp_paint_share", "opp_paint_fg",
        "opp_mid_range_share", "opp_mid_range_fg",
        "opp_three_share", "opp_three_fg"]},
    {"key": "scoring", "label": "Scoring", "metrics": [
        "pip", "fbp", "scp", "pat", "bench",
        "opp_pip", "opp_fbp", "opp_scp", "opp_pat"]},
]


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


_THREE_ZONES = ("Corner 3", "Wing 3", "Top 3")


def _zone_summary(zones: list[dict], prefix: str = "") -> dict:
    """Share and accuracy for every zone, plus the three-point aggregate."""
    by_name = {z["zone"]: z for z in zones}
    total = sum(z["attempts"] for z in zones)

    out = {}
    for zone in metrics.ZONE_ORDER:
        bucket = by_name[zone]
        out[zone_metric(zone, "share", prefix)] = metrics._pct(bucket["attempts"], total)
        out[zone_metric(zone, "fg", prefix)] = bucket["fg_pct"]
        out[zone_metric(zone, "ppa", prefix)] = bucket["points_per_attempt"]

    three_attempts = sum(by_name[z]["attempts"] for z in _THREE_ZONES)
    three_makes = sum(by_name[z]["makes"] for z in _THREE_ZONES)
    out[f"{prefix}three_share"] = metrics._pct(three_attempts, total)
    out[f"{prefix}three_fg"] = metrics._pct(three_makes, three_attempts)
    return out


def team_metrics(conn, event_slug: str) -> dict:
    """Every rankable number for every team that has played."""
    out: dict[int, dict] = {}
    for team in event_teams(conn, event_slug):
        org_id = team["org_id"]
        profile = team_profile(conn, event_slug, org_id)
        games = profile["games_played"]
        if not games:
            continue
        own, opp = profile["four_factors"], profile["opp_four_factors"]
        t, o = profile["totals"], profile["opp_totals"]

        row = {
            "pace": profile["pace"],
            "off_rating": own.get("off_rating"),
            "def_rating": own.get("def_rating"),
            "net_rating": own.get("net_rating"),
            "efg_pct": own.get("efg_pct"),
            "tov_pct": own.get("tov_pct"),
            "oreb_pct": own.get("oreb_pct"),
            "ft_rate": own.get("ft_rate"),
            "opp_efg_pct": opp.get("efg_pct"),
            "tov_forced_pct": opp.get("tov_pct"),
            "dreb_pct": own.get("dreb_pct"),
            "opp_ft_rate": opp.get("ft_rate"),
            "fg3_rate": own.get("fg3_rate"),
            "pts": t["PTS"] / games,
            "fg_pct": metrics._pct(t["FGM"], t["FGA"]),
            "fg3_pct": metrics._pct(t["FG3M"], t["FG3A"]),
            "ft_pct": metrics._pct(t["FTM"], t["FTA"]),
            "oreb": t["OR"] / games,
            "dreb": t["DR"] / games,
            "ast": t["AS"] / games,
            "tov": t["TO"] / games,
            "stl": t["ST"] / games,
            "blk": t["BS"] / games,
            "pf": t["PF"] / games,
            "opp_pts": o["PTS"] / games,
            "opp_fg_pct": metrics._pct(o["FGM"], o["FGA"]),
            "opp_fg3_pct": metrics._pct(o["FG3M"], o["FG3A"]),
            "opp_ft_pct": metrics._pct(o["FTM"], o["FTA"]),
            "opp_oreb": o["OR"] / games,
            "opp_dreb": o["DR"] / games,
            "opp_ast": o["AS"] / games,
            "opp_tov": o["TO"] / games,
            "opp_stl": o["ST"] / games,
            "opp_blk": o["BS"] / games,
            "opp_pf": o["PF"] / games,
            "pip": t["A_PIP"] / games,
            "fbp": t["A_FBP"] / games,
            "scp": t["A_SCP"] / games,
            "pat": t["A_PAT"] / games,
            "bench": t["A_PFB"] / games,
            "opp_pip": o["A_PIP"] / games,
            "opp_fbp": o["A_FBP"] / games,
            "opp_scp": o["A_SCP"] / games,
            "opp_pat": o["A_PAT"] / games,
        }
        row.update(_zone_summary(metrics.zone_breakdown(shots(conn, event_slug, org_id))))
        row.update(_zone_summary(
            metrics.zone_breakdown(shots_faced(conn, event_slug, org_id)), "opp_"))
        out[org_id] = row
    return out


def _rank_values(pairs: list[tuple], higher_is_better) -> dict:
    """Rank a list of (key, value), ties sharing a rank."""
    if higher_is_better is None:
        pairs = sorted(pairs, key=lambda pair: -pair[1])
    else:
        pairs = sorted(pairs, key=lambda pair: -pair[1] if higher_is_better else pair[1])
    total = len(pairs)
    ranks = {}
    previous_value = None
    previous_rank = 0
    for index, (key, value) in enumerate(pairs, start=1):
        rank = previous_rank if value == previous_value else index
        previous_value, previous_rank = value, rank
        # Percentile of the field this entry beats, and a five-step tier used for
        # shading. Metrics with no direction get no tier, so they are never
        # coloured as if one end were good.
        percentile = 100.0 * (total - rank) / (total - 1) if total > 1 else 50.0
        tier = None
        if higher_is_better is not None:
            tier = 1 if percentile >= 80 else 2 if percentile >= 60 else \
                3 if percentile >= 40 else 4 if percentile >= 20 else 5
        ranks[key] = {"rank": rank, "of": total, "label": f"{ordinal(rank)} of {total}",
                      "percentile": percentile, "tier": tier}
    return ranks


def event_ranks(conn, event_slug: str) -> dict:
    """{org_id: {metric: {rank, of, label, percentile, tier}}} across the event."""
    values = team_metrics(conn, event_slug)
    ranks: dict[int, dict] = {org_id: {} for org_id in values}
    for metric, spec in TEAM_METRICS.items():
        pairs = [(org_id, row[metric]) for org_id, row in values.items()
                 if row.get(metric) is not None]
        if not pairs:
            continue
        for org_id, entry in _rank_values(pairs, spec["better"]).items():
            ranks[org_id][metric] = entry
    return ranks


PLAYER_METRICS: dict[str, bool | None] = {
    "minutes_pg": True, "pts_pg": True, "reb_pg": True, "ast_pg": True,
    "fg_pct": True, "fg3_pct": True, "ft_pct": True, "pts_per_fga": True,
    "usage_pct": None, "ts_pct": True, "efg_pct": True,
    "treb_pct": True, "ast_pct": True, "tov_pct": False,
    "fouls_drawn_per40": True,
}

# Every player who has been on the floor is ranked, so no row of a player table
# is left blank. Shooting percentages are the exception: without a volume floor
# a player who went one-for-one leads the event, which is worse than showing
# nothing. The floor grows with games played, so it still bites after game one.
# The attempt counter each shooting metric is judged on, and the per-game rate a
# player must clear to be ranked on it.
SHOOTING_RANK_GATES: dict[str, tuple[str, float]] = {
    "fg_pct": ("FGA", 2.0),
    "efg_pct": ("FGA", 2.0),
    "ts_pct": ("FGA", 2.0),
    "pts_per_fga": ("FGA", 2.0),
    "fg3_pct": ("FG3A", 1.0),
    "ft_pct": ("FTA", 1.0),
}
MIN_SHOOTING_ATTEMPTS = 3


def ranks_shooting(player: dict, metric: str) -> bool:
    """Has this player shot enough for a percentile on `metric` to mean anything?"""
    gate = SHOOTING_RANK_GATES.get(metric)
    if gate is None:
        return True
    counter, per_game = gate
    floor = max(MIN_SHOOTING_ATTEMPTS, per_game * (player.get("games") or 1))
    return (player.get(counter) or 0) >= floor


def player_percentiles(conn, event_slug: str) -> dict:
    """Percentile of every player in the event against the whole field.

    Returns {person_id: {metric: {...}}} plus a "_pool" key describing what the
    numbers are measured against, so a report can say so.
    """
    pool = [player
            for team in event_teams(conn, event_slug)
            for player in player_profile(conn, event_slug, team["org_id"])]

    out: dict = {p["person_id"]: {} for p in pool}
    for metric, better in PLAYER_METRICS.items():
        pairs = [(p["person_id"], p[metric]) for p in pool
                 if p.get(metric) is not None and ranks_shooting(p, metric)]
        if not pairs:
            continue
        for person_id, entry in _rank_values(pairs, better).items():
            out[person_id][metric] = entry
    out["_pool"] = {"size": len(pool), "min_attempts": MIN_SHOOTING_ATTEMPTS}
    return out


def player_profile(conn, event_slug: str, org_id: int,
                   game_ids: list[int] | None = None) -> list[dict]:
    """Per-player totals and rate stats over the selected games."""
    query = (
        "SELECT * FROM player_game_stats WHERE event_slug=? AND org_id=?"
    )
    params: list = [event_slug, org_id]
    if game_ids is not None:
        if not game_ids:
            return []
        query += f" AND game_id IN ({','.join('?' * len(game_ids))})"
        params.extend(game_ids)
    rows = [dict(r) for r in conn.execute(query, params)]
    if not rows:
        return []

    team_rows = []
    opp_rows = []
    ids = sorted({r["game_id"] for r in rows})
    for game_id in ids:
        team_rows.append(dict(conn.execute(
            "SELECT * FROM team_game_stats WHERE event_slug=? AND game_id=? AND org_id=?",
            (event_slug, game_id, org_id)).fetchone()))
        opp_rows.append(dict(conn.execute(
            "SELECT * FROM team_game_stats WHERE event_slug=? AND game_id=? AND org_id!=?",
            (event_slug, game_id, org_id)).fetchone()))

    team_total = _sum_rows(team_rows, _TEAM_STAT_KEYS)
    opp_total = _sum_rows(opp_rows, _TEAM_STAT_KEYS)

    # Team player-seconds available across these games.
    team_seconds = 0
    for game_id in ids:
        length = conn.execute(
            "SELECT MAX(game_seconds) AS s FROM pbp_events WHERE event_slug=? AND game_id=?",
            (event_slug, game_id)).fetchone()["s"] or 4 * PERIOD_SECONDS
        team_seconds += 5 * length

    by_player: dict[int, dict] = {}
    for row in rows:
        entry = by_player.setdefault(
            row["person_id"],
            {
                "person_id": row["person_id"],
                "full_name": row["full_name"],
                "uniform_number": row["uniform_number"],
                "position": row["position"],
                "games": 0,
                "starts": 0,
                "seconds_played": 0,
                **{k: 0 for k in _PLAYER_STAT_KEYS},
            },
        )
        if row["has_played"]:
            entry["games"] += 1
        entry["starts"] += row["starter"] or 0
        entry["seconds_played"] += row["seconds_played"] or 0
        for key in _PLAYER_STAT_KEYS:
            if row.get(key) is not None:
                entry[key] += row[key]

    out = []
    for entry in by_player.values():
        if entry["games"] == 0:
            continue
        advanced = metrics.player_advanced(entry, team_total, opp_total, team_seconds)
        entry.update(advanced)
        entry["minutes_pg"] = entry["seconds_played"] / 60.0 / entry["games"]
        entry["pts_pg"] = entry["PTS"] / entry["games"]
        entry["reb_pg"] = entry["REB"] / entry["games"]
        entry["ast_pg"] = entry["AS"] / entry["games"]
        entry["fg_pct"] = metrics._pct(entry["FGM"], entry["FGA"])
        # Points produced per field goal attempt. Exactly twice eFG%, but on a
        # scale coaches read directly: 1.00 means a shot is worth a point.
        entry["pts_per_fga"] = metrics._safe_div(
            2 * entry["FG2M"] + 3 * entry["FG3M"], entry["FGA"])
        entry["fg3_pct"] = metrics._pct(entry["FG3M"], entry["FG3A"])
        entry["ft_pct"] = metrics._pct(entry["FTM"], entry["FTA"])
        entry["fouls_drawn_per40"] = metrics._safe_div(entry["FD"] * 2400.0,
                                                      entry["seconds_played"])
        out.append(entry)

    out.sort(key=lambda p: -p["seconds_played"])
    return out


def shots(conn, event_slug: str, org_id: int,
          game_ids: list[int] | None = None,
          person_id: int | None = None) -> list[dict]:
    """Field goal attempts taken by a team, or by one player."""
    query = (
        "SELECT * FROM pbp_events WHERE event_slug=? AND org_id=? "
        "AND action_code IN ('P2','P3')"
    )
    params: list = [event_slug, org_id]
    if person_id is not None:
        query += " AND person_id=?"
        params.append(person_id)
    if game_ids is not None:
        if not game_ids:
            return []
        query += f" AND game_id IN ({','.join('?' * len(game_ids))})"
        params.extend(game_ids)
    return [dict(r) for r in conn.execute(query, params)]


def shots_faced(conn, event_slug: str, org_id: int,
                game_ids: list[int] | None = None) -> list[dict]:
    """Field goal attempts opponents took against this team.

    The defensive twin of `shots`: same games, everyone except us. Where a team
    is allowed to shoot from says as much as where it chooses to shoot.
    """
    if game_ids is None:
        game_ids = [g["game_id"] for g in team_games(conn, event_slug, org_id)]
    if not game_ids:
        return []
    placeholders = ",".join("?" * len(game_ids))
    query = (
        f"SELECT * FROM pbp_events WHERE event_slug=? AND org_id!=? "
        f"AND action_code IN ('P2','P3') AND game_id IN ({placeholders})"
    )
    return [dict(r) for r in conn.execute(query, [event_slug, org_id, *game_ids])]


def lineup_profile(conn, event_slug: str, org_id: int,
                   game_ids: list[int] | None = None,
                   min_seconds: int = 120) -> list[dict]:
    """Most-used lineups, with the sample size that qualifies them.

    Games whose reconstruction failed validation are excluded outright rather
    than blended in.
    """
    query = (
        "SELECT s.* FROM stints s "
        "JOIN games g ON g.event_slug=s.event_slug AND g.game_id=s.game_id "
        "WHERE s.event_slug=? AND s.org_id=? AND g.lineups_ok=1"
    )
    params: list = [event_slug, org_id]
    if game_ids is not None:
        if not game_ids:
            return []
        query += f" AND s.game_id IN ({','.join('?' * len(game_ids))})"
        params.extend(game_ids)

    totals: dict[str, dict] = {}
    for row in conn.execute(query, params):
        entry = totals.setdefault(
            row["lineup_key"],
            {"lineup_key": row["lineup_key"], "seconds": 0, "points_for": 0,
             "points_against": 0, "stints": 0, "games": set()},
        )
        entry["seconds"] += row["seconds"] or 0
        entry["points_for"] += row["points_for"] or 0
        entry["points_against"] += row["points_against"] or 0
        entry["stints"] += 1
        entry["games"].add(row["game_id"])

    names = _player_names(conn)
    out = []
    for entry in totals.values():
        if entry["seconds"] < min_seconds:
            continue
        ids = [int(p) for p in entry["lineup_key"].split(",") if p]
        entry["players"] = [names.get(pid, {"full_name": str(pid),
                                            "uniform_number": ""}) for pid in ids]
        entry["minutes"] = entry["seconds"] / 60.0
        entry["plus_minus"] = entry["points_for"] - entry["points_against"]
        entry["games"] = len(entry["games"])
        possessions_estimate = entry["seconds"] / 60.0 * 2.0  # ~2 possessions/minute
        entry["net_per_100"] = (
            entry["plus_minus"] / possessions_estimate * 100.0
            if entry["seconds"] >= MIN_RATE_SECONDS and possessions_estimate else None
        )
        out.append(entry)

    out.sort(key=lambda e: -e["seconds"])
    return out


def _player_names(conn) -> dict[int, dict]:
    return {
        row["person_id"]: dict(row)
        for row in conn.execute(
            "SELECT person_id, full_name, uniform_number, position FROM players")
    }


def on_off(conn, event_slug: str, org_id: int,
           game_ids: list[int] | None = None) -> list[dict]:
    """Team margin per 100 with each player on court versus off it."""
    query = (
        "SELECT s.* FROM stints s "
        "JOIN games g ON g.event_slug=s.event_slug AND g.game_id=s.game_id "
        "WHERE s.event_slug=? AND s.org_id=? AND g.lineups_ok=1"
    )
    params: list = [event_slug, org_id]
    if game_ids is not None:
        if not game_ids:
            return []
        query += f" AND s.game_id IN ({','.join('?' * len(game_ids))})"
        params.extend(game_ids)

    stints = [dict(r) for r in conn.execute(query, params)]
    if not stints:
        return []

    roster = set()
    for stint in stints:
        roster.update(int(p) for p in stint["lineup_key"].split(",") if p)

    names = _player_names(conn)
    out = []
    for person_id in roster:
        on = {"seconds": 0, "for": 0, "against": 0}
        off = {"seconds": 0, "for": 0, "against": 0}
        for stint in stints:
            ids = {int(p) for p in stint["lineup_key"].split(",") if p}
            bucket = on if person_id in ids else off
            bucket["seconds"] += stint["seconds"] or 0
            bucket["for"] += stint["points_for"] or 0
            bucket["against"] += stint["points_against"] or 0

        def per100(bucket):
            possessions_estimate = bucket["seconds"] / 60.0 * 2.0
            if not possessions_estimate or bucket["seconds"] < MIN_RATE_SECONDS:
                return None
            return (bucket["for"] - bucket["against"]) / possessions_estimate * 100.0

        on_rating, off_rating = per100(on), per100(off)
        out.append(
            {
                "person_id": person_id,
                "full_name": names.get(person_id, {}).get("full_name", str(person_id)),
                "uniform_number": names.get(person_id, {}).get("uniform_number", ""),
                "on_minutes": on["seconds"] / 60.0,
                "off_minutes": off["seconds"] / 60.0,
                "on_net": on_rating,
                "off_net": off_rating,
                "swing": (on_rating - off_rating)
                if on_rating is not None and off_rating is not None else None,
            }
        )

    out.sort(key=lambda e: -e["on_minutes"])
    return out


def quarter_scores(conn, event_slug: str, game_id: int) -> dict:
    rows = conn.execute(
        "SELECT org_id, period, score FROM team_periods "
        "WHERE event_slug=? AND game_id=? ORDER BY period",
        (event_slug, game_id),
    )
    by_org: dict[int, dict] = defaultdict(dict)
    for row in rows:
        by_org[row["org_id"]][row["period"]] = row["score"]
    return dict(by_org)


def score_timeline(conn, event_slug: str, game_id: int, org_id: int) -> list[dict]:
    """Margin from the subject team's point of view, over elapsed time."""
    game = conn.execute(
        "SELECT team_a_org_id FROM games WHERE event_slug=? AND game_id=?",
        (event_slug, game_id)).fetchone()
    subject_is_a = game["team_a_org_id"] == org_id

    points = [{"seconds": 0, "margin": 0}]
    for row in conn.execute(
        "SELECT game_seconds, score_a, score_b FROM pbp_events "
        "WHERE event_slug=? AND game_id=? AND game_seconds IS NOT NULL "
        'ORDER BY "order"',
        (event_slug, game_id),
    ):
        margin = ((row["score_a"] or 0) - (row["score_b"] or 0)) if subject_is_a \
            else ((row["score_b"] or 0) - (row["score_a"] or 0))
        points.append({"seconds": row["game_seconds"], "margin": margin})
    return points


def starters_vs_bench(conn, event_slug: str, org_id: int,
                      game_ids: list[int] | None = None) -> dict:
    """How much of the scoring comes off the bench."""
    query = ("SELECT starter, SUM(PTS) AS pts, SUM(seconds_played) AS secs "
             "FROM player_game_stats WHERE event_slug=? AND org_id=?")
    params: list = [event_slug, org_id]
    if game_ids is not None:
        if not game_ids:
            return {}
        query += f" AND game_id IN ({','.join('?' * len(game_ids))})"
        params.extend(game_ids)
    query += " GROUP BY starter"

    result = {"starter_pts": 0, "bench_pts": 0, "starter_min": 0.0, "bench_min": 0.0}
    for row in conn.execute(query, params):
        if row["starter"]:
            result["starter_pts"] = row["pts"] or 0
            result["starter_min"] = (row["secs"] or 0) / 60.0
        else:
            result["bench_pts"] = row["pts"] or 0
            result["bench_min"] = (row["secs"] or 0) / 60.0
    total = result["starter_pts"] + result["bench_pts"]
    result["bench_share_pct"] = metrics._pct(result["bench_pts"], total)
    return result


def event_fixtures(conn, event_slug: str) -> list[dict]:
    """Every fixture in the event, played or not, in tip order.

    The schedule feed carries all 81 games from the day the event page opens, so
    this is complete before a ball is thrown. Scores come from the schedule
    rather than the box score because `validate_game` already asserts the two
    agree, and an unplayed game reports 0-0, which is why they are only read once
    the game has actually been ingested.
    """
    out = []
    for row in conn.execute(
        "SELECT * FROM games WHERE event_slug=? ORDER BY game_utc, game_id",
        (event_slug,),
    ):
        played = row["parsed_at"] is not None
        out.append({
            "game_id": row["game_id"],
            "game_utc": row["game_utc"],
            "game_datetime": row["game_datetime"],
            "played": played,
            "is_live": bool(row["is_live"]),
            "venue": row["venue_name"],
            "city": row["host_city"],
            "home": {"org_id": row["team_a_org_id"], "code": row["team_a_code"],
                     "name": row["team_a_name"],
                     "score": row["team_a_score"] if played else None},
            "away": {"org_id": row["team_b_org_id"], "code": row["team_b_code"],
                     "name": row["team_b_name"],
                     "score": row["team_b_score"] if played else None},
        })
    return out


def next_opponent(conn, event_slug: str, org_id: int) -> dict | None:
    """The team's next unplayed fixture, which is what a scout report targets."""
    row = conn.execute(
        """
        SELECT game_id, game_utc, game_datetime, status_code,
               team_a_org_id, team_b_org_id, team_a_code, team_b_code,
               host_city, venue_name
        FROM games
        WHERE event_slug=? AND (team_a_org_id=? OR team_b_org_id=?)
          AND parsed_at IS NULL
        ORDER BY game_utc
        LIMIT 1
        """,
        (event_slug, org_id, org_id),
    ).fetchone()
    if row is None:
        return None
    opp_id = row["team_b_org_id"] if row["team_a_org_id"] == org_id else row["team_a_org_id"]
    return {
        "game_id": row["game_id"],
        "game_utc": row["game_utc"],
        "game_datetime": row["game_datetime"],
        "opponent_org_id": opp_id,
        "host_city": row["host_city"],
        "venue_name": row["venue_name"],
    }
