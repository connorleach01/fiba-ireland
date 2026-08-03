"""Render self-contained HTML reports."""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import analysis, charts, metrics, theming
from .config import IRELAND_ORG_ID, REPORTS_DIR, TEMPLATES_DIR

log = logging.getLogger(__name__)
IRISH_TZ = ZoneInfo("Europe/Dublin")

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _flag(code, width: int = 16):
    from markupsafe import Markup
    return Markup(theming.flag_svg(code, width))


_env.globals["flag"] = _flag


def scout_filename(code: str) -> str:
    return f"scout_{code}.html"


def build_nav(conn, event_slug: str, org_id: int) -> dict:
    """The site's navigation model.

    Filenames are derived the same way the builders derive them, so the nav can
    be assembled before anything is rendered and every page can link to every
    other page.
    """
    subject = analysis.team_identity(conn, event_slug, org_id)

    scouts = []
    for team in analysis.event_teams(conn, event_slug):
        if team["org_id"] == org_id or not team["games"]:
            continue
        scouts.append({"code": team["code"], "name": team["name"],
                       "href": scout_filename(team["code"])})
    scouts.sort(key=lambda r: r["name"])

    reviews = []
    for row in conn.execute(
        "SELECT g.game_id, g.game_utc, g.game_datetime, t.opp_org_id, t.score, "
        "       t.opp_score, t.won "
        "FROM games g JOIN team_game_stats t "
        "  ON t.event_slug=g.event_slug AND t.game_id=g.game_id "
        "WHERE g.event_slug=? AND t.org_id=? AND g.parsed_at IS NOT NULL "
        "ORDER BY g.game_utc DESC",
        (event_slug, org_id),
    ):
        other = analysis.team_identity(conn, event_slug, row["opp_org_id"])
        reviews.append({
            "code": other["code"],
            "label": f"v {other['name']} "
                     f"({'W' if row['won'] else 'L'} {row['score']}-{row['opp_score']})",
            "href": game_filename(conn, event_slug, row["game_id"]),
        })

    tournament = (f"{subject['code'].lower()}_tournament.html"
                  if analysis.team_profile(conn, event_slug, org_id)["games_played"]
                  else None)

    return {"subject": subject, "scouts": scouts, "reviews": reviews,
            "tournament": tournament, "teams": "tournament_teams.html",
            "schedule": "schedule.html"}


def _played_in(conn, event_slug: str, game_id: int, org_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM team_game_stats WHERE event_slug=? AND game_id=? AND org_id=?",
        (event_slug, game_id, org_id)).fetchone() is not None


def _box_rows(entries, ranked: bool = False) -> list[dict]:
    """Rows for the raw team box score and the scoring breakdown.

    `entries` is a list of (identity, stats, games, highlight); percentages are
    always recomputed from the totals rather than averaged. `ranked` marks the
    first row as the team's own line and the second as what it allowed, which is
    what tells the scoring table whether to look up `pip` or `opp_pip`. A game
    sheet's two rows are two different teams, so it leaves this off.
    """
    rows = [
        {"code": identity["code"], "name": identity["name"], "stats": stats,
         "games": games, "highlight": highlight, "metric_prefix": None}
        for identity, stats, games, highlight in entries
    ]
    if ranked:
        for row, prefix in zip(rows, ("", "opp_")):
            row["metric_prefix"] = prefix
    return rows


def _player_details(conn, event_slug: str, org_id: int, players: list[dict],
                    game_ids: list[int] | None = None) -> dict:
    """Pre-render the expandable panel for each player.

    Built at generation time rather than fetched on click, so the page stays a
    single self-contained file that works offline and prints whatever is open.
    Scope follows the page: a scouting report shows every game the player has
    played, a game review shows only that game.
    """
    from markupsafe import Markup

    details = {}
    for player in players:
        shots = analysis.shots(conn, event_slug, org_id, game_ids,
                               person_id=player["person_id"])
        zones = metrics.zone_breakdown(shots)
        chart = charts.shot_chart(
            shots, title=f"{player['full_name']} shot chart", width=228)
        details[player["person_id"]] = {
            "chart": Markup(chart),
            "zones": zones,
            "attempts": sum(z["attempts"] for z in zones),
        }
    return details


def _inks(subject_code: str | None, other_code: str | None) -> dict:
    """Chart colours for the two teams on a page.

    Country colours are used wherever they survive the contrast and colour
    vision checks in `theming`; the opponent's hue is lightness-shifted rather
    than swapped when the two are too close.
    """
    ink_a, ink_b, substituted = theming.pick_pair(subject_code, other_code)
    return {"ink_a": ink_a, "ink_b": ink_b, "ink_substituted": substituted}


def _now_text() -> str:
    return dt.datetime.now(IRISH_TZ).strftime("%a %d %b %Y, %H:%M") + " (Irish time)"


def _local(iso_utc: str | None, fallback: str | None = None) -> str:
    """Render a stored UTC timestamp in Irish time for the staff reading it."""
    if not iso_utc:
        return fallback or ""
    try:
        moment = dt.datetime.fromisoformat(iso_utc).astimezone(IRISH_TZ)
    except ValueError:
        return fallback or ""
    return moment.strftime("%a %d %b, %H:%M")


def _event_name(conn, event_slug: str) -> str:
    return event_slug.replace("-", " ").title().replace("Fiba", "FIBA") \
        .replace("U16", "U16").replace("U18", "U18")


def _write(name: str, html: str) -> str:
    path = REPORTS_DIR / name
    path.write_text(html, encoding="utf-8")
    log.info("wrote %s", path)
    return name


def _mark(value):
    """Jinja receives pre-rendered SVG, which must not be escaped again."""
    from markupsafe import Markup
    return Markup(value)


# --------------------------------------------------------------------------


def build_scout(conn, event_slug: str, org_id: int,
                subject_org_id: int = IRELAND_ORG_ID,
                fixture_text: str | None = None, nav: dict | None = None,
                reference: str | None = None, context: dict | None = None) -> str | None:
    """Opponent profile: what they do, who does it, and how they line up."""
    profile = analysis.team_profile(conn, event_slug, org_id)
    if profile["games_played"] == 0:
        return None

    league = analysis.event_averages(conn, event_slug)
    players = analysis.player_profile(conn, event_slug, org_id)
    team_shots = analysis.shots(conn, event_slug, org_id)
    faced_shots = analysis.shots_faced(conn, event_slug, org_id)
    zones = metrics.zone_breakdown(team_shots)
    faced_zones = metrics.zone_breakdown(faced_shots)
    lineups = analysis.lineup_profile(conn, event_slug, org_id)
    bench = analysis.starters_vs_bench(conn, event_slug, org_id)
    details = _player_details(conn, event_slug, org_id, players)

    lineups_available = any(
        g["lineups_ok"] for g in profile["games"] if g["lineups_ok"] is not None
    )
    # Every game in the event has a sheet, so this team's log links out as well.
    for game in profile["games"]:
        game["review_href"] = game_filename(conn, event_slug, game["game_id"])

    html = _env.get_template("scout.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        profile=profile,
        league=league,
        players=players,
        zones=zones, faced_zones=faced_zones, player_details=details,
        box_rows=_box_rows([
            (profile["identity"], profile["totals"], profile["games_played"], True),
            ({"code": "OPP", "name": "Their opponents"}, profile["opp_totals"],
             profile["games_played"], False)], ranked=True),
        lineups=lineups[:8],
        lineups_available=lineups_available,
        bench=bench,
        fixture=fixture_text,
        ranks=(context or {}).get("ranks", {}).get(org_id, {}),
        percentiles=(context or {}).get("percentiles", {}),
        nav=nav, page="scout", reference=reference,
        **_inks(profile["identity"]["code"], None),
        charts={
            "four_factors": _mark(charts.four_factor_bars(
                profile["four_factors"], profile["opp_four_factors"], league,
                team_label=profile["identity"]["code"], opp_label="Opponents")),
            "zones": _mark(charts.zone_bars(zones)),
            "shots": _mark(charts.shot_chart(
                team_shots, title=f"{profile['identity']['name']} shot chart")),
            "shots_faced": _mark(charts.shot_chart(
                faced_shots, made_label="Opponent made",
                title=f"Shots allowed by {profile['identity']['name']}")),
        },
    )
    code = profile["identity"]["code"]
    return _write(f"scout_{code}.html", html)


def game_filename(conn, event_slug: str, game_id: int) -> str:
    """One file per game, named from the fixture rather than from a viewpoint.

    Both teams' game logs link here, so the name must not depend on who is
    looking at it.
    """
    row = conn.execute(
        "SELECT game_utc, game_datetime, team_a_code, team_b_code "
        "FROM games WHERE event_slug=? AND game_id=?",
        (event_slug, game_id)).fetchone()
    if row is None:
        return f"game_{game_id}.html"
    date_part = (row["game_utc"] or row["game_datetime"] or "")[:10] or str(game_id)
    return f"{date_part}_{row['team_a_code']}-v-{row['team_b_code']}_game.html"


def _side(conn, event_slug: str, game_id: int, org_id: int, opp_org_id: int) -> dict:
    """Everything one team contributes to a game sheet."""
    row = conn.execute(
        "SELECT * FROM team_game_stats WHERE event_slug=? AND game_id=? AND org_id=?",
        (event_slug, game_id, org_id)).fetchone()
    opp = conn.execute(
        "SELECT * FROM team_game_stats WHERE event_slug=? AND game_id=? AND org_id=?",
        (event_slug, game_id, opp_org_id)).fetchone()
    totals, opp_totals = dict(row), dict(opp)
    players = analysis.player_profile(conn, event_slug, org_id, [game_id])
    shots = analysis.shots(conn, event_slug, org_id, [game_id])
    return {
        "identity": analysis.team_identity(conn, event_slug, org_id),
        "totals": totals,
        "opp_totals": opp_totals,
        "score": row["score"],
        "opp_score": row["opp_score"],
        "won": bool(row["won"]),
        "ff": metrics.four_factors(totals, opp_totals),
        "off": metrics.four_factors(opp_totals, totals),
        "players": players,
        "details": _player_details(conn, event_slug, org_id, players, [game_id]),
        "zones": metrics.zone_breakdown(shots),
        "chart": _mark(charts.shot_chart(shots, width=300)),
        "lineups": analysis.lineup_profile(conn, event_slug, org_id, [game_id],
                                           min_seconds=90),
        "bench": analysis.starters_vs_bench(conn, event_slug, org_id, [game_id]),
    }


def build_review(conn, event_slug: str, game_id: int,
                 org_id: int = IRELAND_ORG_ID, nav: dict | None = None,
                 reference: str | None = None) -> str | None:
    """A sheet for one game, covering both teams.

    Built once per game rather than once per viewpoint: every team's game log
    links to the same page, and a coach looking at a fixture wants both sides of
    it anyway. `org_id` only decides which team is listed first, so Ireland leads
    on its own games.
    """
    game = conn.execute(
        "SELECT * FROM games WHERE event_slug=? AND game_id=? AND parsed_at IS NOT NULL",
        (event_slug, game_id)).fetchone()
    if game is None:
        return None

    a_id, b_id = game["team_a_org_id"], game["team_b_org_id"]
    # Put the subject team first when it played, otherwise keep fixture order.
    if org_id == b_id:
        a_id, b_id = b_id, a_id

    home = _side(conn, event_slug, game_id, a_id, b_id)
    away = _side(conn, event_slug, game_id, b_id, a_id)
    league = analysis.event_averages(conn, event_slug)

    quarters = analysis.quarter_scores(conn, event_slug, game_id)
    home_periods = quarters.get(a_id, {})
    away_periods = quarters.get(b_id, {})
    period_labels = sorted(set(home_periods) | set(away_periods))

    box_rows = _box_rows([(home["identity"], home["totals"], 1, True),
                          (away["identity"], away["totals"], 1, False)])

    html = _env.get_template("review.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        home=home, away=away,
        us=home["identity"], them=away["identity"],
        score=home["score"], opp_score=home["opp_score"], won=home["won"],
        ff=home["ff"], off=home["off"],
        totals=home["totals"], opp_totals=home["opp_totals"],
        played_at=_local(game["game_utc"], game["game_datetime"]),
        venue=game["venue_name"],
        league=league, box_rows=box_rows,
        periods=period_labels,
        our_periods=home_periods, their_periods=away_periods,
        lineups_available=bool(game["lineups_ok"]),
        nav=nav, page="review", reference=reference,
        charts={
            "four_factors": _mark(charts.four_factor_bars(
                home["ff"], away["ff"], league,
                team_label=home["identity"]["code"],
                opp_label=away["identity"]["code"])),
            "timeline": _mark(charts.margin_timeline(
                analysis.score_timeline(conn, event_slug, game_id, a_id),
                team_label=home["identity"]["code"],
                periods=max(4, len(period_labels)))),
        },
        **_inks(home["identity"]["code"], away["identity"]["code"]),
    )
    return _write(game_filename(conn, event_slug, game_id), html)


def build_tournament(conn, event_slug: str, org_id: int = IRELAND_ORG_ID,
                     nav: dict | None = None, reference: str | None = None,
                     context: dict | None = None) -> str | None:
    """Cumulative view that grows as the event goes on."""
    profile = analysis.team_profile(conn, event_slug, org_id)
    if profile["games_played"] == 0:
        return None

    league = analysis.event_averages(conn, event_slug)
    players = analysis.player_profile(conn, event_slug, org_id)
    team_shots = analysis.shots(conn, event_slug, org_id)
    faced_shots = analysis.shots_faced(conn, event_slug, org_id)
    zones = metrics.zone_breakdown(team_shots)
    faced_zones = metrics.zone_breakdown(faced_shots)
    lineups = analysis.lineup_profile(conn, event_slug, org_id)

    # The game log is the way back into an individual game now that the nav
    # carries no Games menu, so each row links to its own review.
    for game in profile["games"]:
        game["review_href"] = game_filename(conn, event_slug, game["game_id"])

    lineups_available = any(
        g["lineups_ok"] for g in profile["games"] if g["lineups_ok"] is not None)

    html = _env.get_template("tournament.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        profile=profile, league=league, players=players, zones=zones,
        faced_zones=faced_zones,
        ranks=(context or {}).get("ranks", {}).get(org_id, {}),
        percentiles=(context or {}).get("percentiles", {}),
        player_details=_player_details(conn, event_slug, org_id, players),
        box_rows=_box_rows([
            (profile["identity"], profile["totals"], profile["games_played"], True),
            ({"code": "OPP", "name": "Opponents"}, profile["opp_totals"],
             profile["games_played"], False)], ranked=True),
        lineups=lineups[:10], lineups_available=lineups_available,
        nav=nav, page="tournament", reference=reference,
        **_inks(profile["identity"]["code"], None),
        charts={
            "four_factors": _mark(charts.four_factor_bars(
                profile["four_factors"], profile["opp_four_factors"], league,
                team_label=profile["identity"]["code"], opp_label="Opponents")),
            "zones": _mark(charts.zone_bars(zones)),
            "shots": _mark(charts.shot_chart(team_shots, title="Shot chart")),
            "shots_faced": _mark(charts.shot_chart(
                faced_shots, made_label="Opponent made",
                title="Shots allowed")),
        },
    )
    return _write(f"{profile['identity']['code'].lower()}_tournament.html", html)


def build_schedule(conn, event_slug: str, org_id: int = IRELAND_ORG_ID,
                   nav: dict | None = None, reference: str | None = None) -> str:
    """The fixture list, day by day, results filled in as they land.

    Grouping happens here rather than in `analysis` because the day a game
    belongs to is the day it is in Irish time, and the timezone is a presentation
    concern. Tip times sit between 09:00 and 19:00 UTC, so nothing straddles
    midnight either way, but the conversion belongs on this side regardless.
    """
    fixtures = analysis.event_fixtures(conn, event_slug)

    # FIBA publishes the knockout rounds before the bracket resolves: no teams,
    # and a stub tip time shared by the whole round. Two games at the same minute
    # is normal, because two venues run in parallel; ten is a placeholder. Those
    # times are suppressed rather than shown, since "23:00" is worse than
    # admitting the slot is not set. Both resolve themselves as the group stage
    # finishes, which is why nothing here is hard-coded to a round or a date.
    from collections import Counter
    per_slot = Counter(f["game_utc"] for f in fixtures if f["game_utc"])
    stub_slots = {slot for slot, n in per_slot.items() if n > 4}

    days: list[dict] = []
    for fixture in fixtures:
        moment = None
        if fixture["game_utc"]:
            try:
                moment = dt.datetime.fromisoformat(
                    fixture["game_utc"]).astimezone(IRISH_TZ)
            except ValueError:
                moment = None

        fixture["confirmed"] = bool(fixture["home"]["code"]
                                    and fixture["away"]["code"])
        timed = moment is not None and fixture["game_utc"] not in stub_slots
        fixture["time"] = moment.strftime("%H:%M") if timed else ""
        fixture["href"] = (game_filename(conn, event_slug, fixture["game_id"])
                           if fixture["played"] else None)
        fixture["subject"] = org_id in (fixture["home"]["org_id"],
                                        fixture["away"]["org_id"])
        # Whose score to embolden. Left None on a draw or an unplayed game.
        home, away = fixture["home"]["score"], fixture["away"]["score"]
        fixture["winner"] = None
        if home is not None and away is not None and home != away:
            fixture["winner"] = "home" if home > away else "away"

        key = moment.date().isoformat() if moment else "tbc"
        if not days or days[-1]["key"] != key:
            days.append({
                "key": key,
                "label": moment.strftime("%A %d %B") if moment else "Date to be confirmed",
                "games": [],
            })
        days[-1]["games"].append(fixture)

    played = sum(1 for d in days for g in d["games"] if g["played"])
    total = sum(len(d["games"]) for d in days)
    unconfirmed = sum(1 for d in days for g in d["games"] if not g["confirmed"])

    html = _env.get_template("schedule.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        subject=analysis.team_identity(conn, event_slug, org_id),
        days=days, played=played, total=total, unconfirmed=unconfirmed,
        nav=nav, page="schedule", reference=reference,
    )
    return _write("schedule.html", html)


def build_teams(conn, event_slug: str,
                highlight_org_id: int = IRELAND_ORG_ID,
                nav: dict | None = None, reference: str | None = None,
                context: dict | None = None) -> str:
    """Leaderboard across every team, in four ranked views.

    Advanced, box score, shooting and scoring breakdown each cover both sides of
    the ball. Every cell carries its rank, which the reader can turn on.
    """
    ranks = (context or {}).get("ranks") or analysis.event_ranks(conn, event_slug)
    values = analysis.team_metrics(conn, event_slug)

    rows = []
    for team in analysis.event_teams(conn, event_slug):
        org_id = team["org_id"]
        if org_id not in values:
            continue
        profile = analysis.team_profile(conn, event_slug, org_id)
        rows.append({
            # Named "stats", not "values": Jinja resolves dot access to
            # attributes first, so `row.values` would find dict.values.
            "identity": profile["identity"],
            "games": profile["games_played"],
            "wins": profile["wins"],
            "losses": profile["losses"],
            "win_pct": profile["wins"] / profile["games_played"],
            "stats": values[org_id],
            "ranks": ranks.get(org_id, {}),
            "highlight": org_id == highlight_org_id,
        })
    rows.sort(key=lambda r: -(r["stats"].get("net_rating") or -999))

    groups = []
    for group in analysis.LEADERBOARD_GROUPS:
        groups.append({
            "key": group["key"],
            "label": group["label"],
            "metrics": [
                {"key": m,
                 "label": analysis.TEAM_METRICS[m]["label"],
                 "dp": analysis.TEAM_METRICS[m].get("dp", 1),
                 "plus": analysis.TEAM_METRICS[m].get("signed", False)}
                for m in group["metrics"]
            ],
        })

    games_played = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE event_slug=? AND parsed_at IS NOT NULL",
        (event_slug,)).fetchone()["n"]

    html = _env.get_template("teams.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        rows=rows, groups=groups,
        league=analysis.event_averages(conn, event_slug),
        games_played=games_played,
        highlight_org_id=highlight_org_id,
        nav=nav, page="teams", reference=reference,
    )
    return _write("tournament_teams.html", html)


def build_index(conn, event_slug: str, org_id: int = IRELAND_ORG_ID,
                entries: dict | None = None, nav: dict | None = None,
                reference: str | None = None) -> str:
    """The dashboard.

    This is the page staff land on, so it answers the three questions they
    actually have before it lists anything: how are we doing, who is next, and
    what happened last time out.
    """
    entries = entries or {}
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE event_slug=?", (event_slug,)
    ).fetchone()["n"]
    done = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE event_slug=? AND parsed_at IS NOT NULL",
        (event_slug,)).fetchone()["n"]

    profile = analysis.team_profile(conn, event_slug, org_id)
    league = analysis.event_averages(conn, event_slug)

    # Next fixture, with a link to that opponent's scouting report.
    upcoming = analysis.next_opponent(conn, event_slug, org_id)
    next_up = None
    if upcoming:
        other = analysis.team_identity(conn, event_slug, upcoming["opponent_org_id"])
        other_profile = analysis.team_profile(conn, event_slug,
                                              upcoming["opponent_org_id"])
        next_up = {
            "identity": other,
            "when": _local(upcoming["game_utc"], upcoming["game_datetime"]),
            "venue": upcoming.get("venue_name") or upcoming.get("host_city"),
            "record": f"{other_profile['wins']}-{other_profile['losses']}",
            "games_played": other_profile["games_played"],
            "net_rating": other_profile["four_factors"].get("net_rating"),
            "pace": other_profile["pace"],
            "scout_href": (scout_filename(other["code"])
                           if other_profile["games_played"] else None),
        }

    # Most recent results, newest first.
    recent = []
    for game in reversed(profile["games"]):
        recent.append({
            "opponent": game["opponent_identity"],
            "won": bool(game["won"]),
            "score": game["score"],
            "opp_score": game["opp_score"],
            "net_rating": game["four_factors"].get("net_rating"),
            "when": _local(game["game_utc"], game["game_datetime"]),
            "href": game_filename(conn, event_slug, game["game_id"]),
        })

    html = _env.get_template("index.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        games_total=total, games_done=done,
        profile=profile, league=league,
        next_up=next_up, recent=recent[:5],
        standing=entries.get("standing", []),
        scouts=entries.get("scouts", []),
        reviews=entries.get("reviews", []),
        nav=nav, page="index", reference=reference,
        # Before the first game the live pages are empty, so point at a filled-in
        # example rather than showing the staff a blank site.
        example_href=("example/index.html"
                      if done == 0 and (REPORTS_DIR / "example" / "index.html").exists()
                      else None),
    )
    return _write("index.html", html)


def _safe(label: str, fn, *args, **kwargs):
    """Run one report build, logging rather than aborting the whole set.

    Mid-tournament, a bug in one template must not cost the staff every other
    report. The failure is loud in the log and the rest still ship.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        log.exception("failed to build %s", label)
        return None


def build_all(conn, event_slug: str, org_id: int = IRELAND_ORG_ID,
              reference: str | None = None) -> dict:
    """Regenerate the full report set. Cheap enough to just always do."""
    written = {"standing": [], "scouts": [], "reviews": []}
    nav = build_nav(conn, event_slug, org_id)
    # Event-wide, so computed once and threaded through rather than recomputed
    # on each of the twenty-odd scouting pages.
    context = {"ranks": analysis.event_ranks(conn, event_slug),
               "percentiles": analysis.player_percentiles(conn, event_slug)}

    tournament = _safe("tournament", build_tournament, conn, event_slug, org_id,
                       nav=nav, reference=reference, context=context)
    if tournament:
        written["standing"].append(
            {"href": tournament, "title": "Ireland: tournament to date",
             "when": "updated now"})
    schedule_page = _safe("schedule", build_schedule, conn, event_slug, org_id,
                          nav=nav, reference=reference)
    if schedule_page:
        written["standing"].append(
            {"href": schedule_page, "title": "Schedule and results",
             "when": "every game, day by day"})
    teams_page = _safe("teams", build_teams, conn, event_slug, org_id, nav=nav,
                       reference=reference, context=context)
    if teams_page:
        written["standing"].append(
            {"href": teams_page,
             "title": "All teams: four factors and ratings", "when": "updated now"})

    # A scout report for the next opponent, and for anyone else with data.
    upcoming = analysis.next_opponent(conn, event_slug, org_id)
    next_opp_id = upcoming["opponent_org_id"] if upcoming else None

    for team in analysis.event_teams(conn, event_slug):
        if team["org_id"] == org_id:
            continue
        fixture = None
        if team["org_id"] == next_opp_id and upcoming:
            fixture = _local(upcoming["game_utc"], upcoming["game_datetime"])
        name = _safe(f"scout {team['org_id']}", build_scout, conn, event_slug,
                     team["org_id"], org_id, fixture, nav=nav,
                     reference=reference, context=context)
        if name:
            other = analysis.team_profile(conn, event_slug, team["org_id"])
            written["scouts"].append({
                "href": name,
                "code": team["code"],
                "name": team["name"],
                "title": f"Scouting: {team['name']}",
                "when": fixture or f"{team['games']} games",
                "is_next": team["org_id"] == next_opp_id,
                "games_played": other["games_played"],
                "wins": other["wins"],
                "losses": other["losses"],
                "win_pct": (other["wins"] / other["games_played"]
                            if other["games_played"] else 0.0),
                "net_rating": other["four_factors"].get("net_rating"),
                "pace": other["pace"],
            })

    # The team we play next sits at the top; the rest rank by net rating, so the
    # list doubles as a read on who is actually good.
    written["scouts"].sort(
        key=lambda r: (0 if r["is_next"] else 1, -(r["net_rating"] or -999)))

    # A sheet for every finished game in the event. Scouting reports link into
    # them from any team's game log, so building only Ireland's would leave most
    # of those links dead.
    for row in conn.execute(
        "SELECT game_id, game_utc, game_datetime, team_a_org_id "
        "FROM games WHERE event_slug=? AND parsed_at IS NOT NULL "
        "ORDER BY game_utc DESC",
        (event_slug,),
    ):
        subject = org_id if _played_in(conn, event_slug, row["game_id"], org_id) \
            else row["team_a_org_id"]
        name = _safe(f"game {row['game_id']}", build_review, conn, event_slug,
                     row["game_id"], subject, nav=nav, reference=reference)
        if name and subject == org_id:
            opponent = conn.execute(
                "SELECT opp_org_id FROM team_game_stats "
                "WHERE event_slug=? AND game_id=? AND org_id=?",
                (event_slug, row["game_id"], org_id)).fetchone()["opp_org_id"]
            identity = analysis.team_identity(conn, event_slug, opponent)
            written["reviews"].append(
                {"href": name, "title": f"Ireland v {identity['name']}",
                 "when": _local(row["game_utc"], row["game_datetime"])})

    _safe("index", build_index, conn, event_slug, org_id, written, nav=nav,
          reference=reference)
    return written
