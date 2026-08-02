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
