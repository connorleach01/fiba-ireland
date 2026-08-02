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


def review_filename(date_part: str, us_code: str, them_code: str) -> str:
    return f"{date_part}_{us_code}-v-{them_code}_review.html"


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
        date_part = (row["game_utc"] or "")[:10] or str(row["game_id"])
        reviews.append({
            "code": other["code"],
            "label": f"v {other['name']} "
                     f"({'W' if row['won'] else 'L'} {row['score']}-{row['opp_score']})",
            "href": review_filename(date_part, subject["code"], other["code"]),
        })

    tournament = (f"{subject['code'].lower()}_tournament.html"
                  if analysis.team_profile(conn, event_slug, org_id)["games_played"]
                  else None)

    return {"subject": subject, "scouts": scouts, "reviews": reviews,
            "tournament": tournament, "teams": "tournament_teams.html"}


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
                reference: str | None = None) -> str | None:
    """Opponent profile: what they do, who does it, and how they line up."""
    profile = analysis.team_profile(conn, event_slug, org_id)
    if profile["games_played"] == 0:
        return None

    league = analysis.event_averages(conn, event_slug)
    players = analysis.player_profile(conn, event_slug, org_id)
    team_shots = analysis.shots(conn, event_slug, org_id)
    zones = metrics.zone_breakdown(team_shots)
    lineups = analysis.lineup_profile(conn, event_slug, org_id)
    bench = analysis.starters_vs_bench(conn, event_slug, org_id)

    lineups_available = any(
        g["lineups_ok"] for g in profile["games"] if g["lineups_ok"] is not None
    )

    html = _env.get_template("scout.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        profile=profile,
        league=league,
        players=players,
        zones=zones,
        lineups=lineups[:8],
        lineups_available=lineups_available,
        bench=bench,
        fixture=fixture_text,
        nav=nav, page="scout", reference=reference,
        **_inks(profile["identity"]["code"], None),
        charts={
            "four_factors": _mark(charts.four_factor_bars(
                profile["four_factors"], profile["opp_four_factors"], league,
                team_label=profile["identity"]["code"], opp_label="Opponents")),
            "zones": _mark(charts.zone_bars(zones)),
            "shots": _mark(charts.shot_chart(
                team_shots, title=f"{profile['identity']['name']} shot chart")),
        },
    )
    code = profile["identity"]["code"]
    return _write(f"scout_{code}.html", html)


def build_review(conn, event_slug: str, game_id: int,
                 org_id: int = IRELAND_ORG_ID, nav: dict | None = None,
                 reference: str | None = None) -> str | None:
    """Self-scout for one game."""
    row = conn.execute(
        "SELECT * FROM team_game_stats WHERE event_slug=? AND game_id=? AND org_id=?",
        (event_slug, game_id, org_id)).fetchone()
    if row is None:
        return None
    opp_row = conn.execute(
        "SELECT * FROM team_game_stats WHERE event_slug=? AND game_id=? AND org_id=?",
        (event_slug, game_id, row["opp_org_id"])).fetchone()
    game = conn.execute(
        "SELECT * FROM games WHERE event_slug=? AND game_id=?",
        (event_slug, game_id)).fetchone()

    totals, opp_totals = dict(row), dict(opp_row)
    us = analysis.team_identity(conn, event_slug, org_id)
    them = analysis.team_identity(conn, event_slug, row["opp_org_id"])
    league = analysis.event_averages(conn, event_slug)

    players = analysis.player_profile(conn, event_slug, org_id, [game_id])
    team_shots = analysis.shots(conn, event_slug, org_id, [game_id])
    zones = metrics.zone_breakdown(team_shots)
    lineups = analysis.lineup_profile(conn, event_slug, org_id, [game_id],
                                      min_seconds=90)
    on_off = analysis.on_off(conn, event_slug, org_id, [game_id])
    bench = analysis.starters_vs_bench(conn, event_slug, org_id, [game_id])
    quarters = analysis.quarter_scores(conn, event_slug, game_id)
    timeline = analysis.score_timeline(conn, event_slug, game_id, org_id)

    our_periods = quarters.get(org_id, {})
    their_periods = quarters.get(row["opp_org_id"], {})
    period_labels = sorted(set(our_periods) | set(their_periods))

    html = _env.get_template("review.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        us=us, them=them,
        score=row["score"], opp_score=row["opp_score"], won=bool(row["won"]),
        played_at=_local(game["game_utc"], game["game_datetime"]),
        venue=game["venue_name"],
        ff=metrics.four_factors(totals, opp_totals),
        off=metrics.four_factors(opp_totals, totals),
        league=league,
        totals=totals, opp_totals=opp_totals,
        players=players, zones=zones,
        lineups=lineups, on_off=on_off, bench=bench,
        lineups_available=bool(game["lineups_ok"]),
        periods=period_labels,
        our_periods=our_periods, their_periods=their_periods,
        nav=nav, page="review", reference=reference,
        **_inks(us["code"], them["code"]),
        charts={
            "four_factors": _mark(charts.four_factor_bars(
                metrics.four_factors(totals, opp_totals),
                metrics.four_factors(opp_totals, totals),
                league, team_label=us["code"], opp_label=them["code"])),
            "zones": _mark(charts.zone_bars(zones)),
            "shots": _mark(charts.shot_chart(team_shots,
                                             title=f"{us['name']} shot chart")),
            "timeline": _mark(charts.margin_timeline(
                timeline, team_label=us["code"],
                periods=max(4, len(period_labels)))),
        },
    )
    date_part = (game["game_utc"] or "")[:10] or str(game_id)
    return _write(f"{date_part}_{us['code']}-v-{them['code']}_review.html", html)


def build_tournament(conn, event_slug: str, org_id: int = IRELAND_ORG_ID,
                     nav: dict | None = None,
                     reference: str | None = None) -> str | None:
    """Cumulative view that grows as the event goes on."""
    profile = analysis.team_profile(conn, event_slug, org_id)
    if profile["games_played"] == 0:
        return None

    league = analysis.event_averages(conn, event_slug)
    players = analysis.player_profile(conn, event_slug, org_id)
    team_shots = analysis.shots(conn, event_slug, org_id)
    zones = metrics.zone_breakdown(team_shots)
    lineups = analysis.lineup_profile(conn, event_slug, org_id)
    lineups_available = any(
        g["lineups_ok"] for g in profile["games"] if g["lineups_ok"] is not None)

    html = _env.get_template("tournament.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        profile=profile, league=league, players=players, zones=zones,
        lineups=lineups[:10], lineups_available=lineups_available,
        nav=nav, page="tournament", reference=reference,
        **_inks(profile["identity"]["code"], None),
        charts={
            "four_factors": _mark(charts.four_factor_bars(
                profile["four_factors"], profile["opp_four_factors"], league,
                team_label=profile["identity"]["code"], opp_label="Opponents")),
            "zones": _mark(charts.zone_bars(zones)),
            "shots": _mark(charts.shot_chart(team_shots, title="Shot chart")),
        },
    )
    return _write(f"{profile['identity']['code'].lower()}_tournament.html", html)


def build_teams(conn, event_slug: str,
                highlight_org_id: int = IRELAND_ORG_ID,
                nav: dict | None = None, reference: str | None = None) -> str:
    """Leaderboard across every team in the event."""
    profiles = []
    for team in analysis.event_teams(conn, event_slug):
        profile = analysis.team_profile(conn, event_slug, team["org_id"])
        if profile["games_played"]:
            profiles.append(profile)
    profiles.sort(key=lambda p: -(p["four_factors"].get("net_rating") or -999))

    games_played = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE event_slug=? AND parsed_at IS NOT NULL",
        (event_slug,)).fetchone()["n"]

    html = _env.get_template("teams.html.j2").render(
        event_name=_event_name(conn, event_slug),
        generated_at=_now_text(),
        teams=profiles,
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
            "href": review_filename(
                (game["game_utc"] or "")[:10] or str(game["game_id"]),
                profile["identity"]["code"], game["opponent_identity"]["code"]),
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

    tournament = _safe("tournament", build_tournament, conn, event_slug, org_id,
                       nav=nav, reference=reference)
    if tournament:
        written["standing"].append(
            {"href": tournament, "title": "Ireland: tournament to date",
             "when": "updated now"})
    teams_page = _safe("teams", build_teams, conn, event_slug, org_id, nav=nav,
                       reference=reference)
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
                     reference=reference)
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

    for row in conn.execute(
        "SELECT g.game_id, g.game_utc, g.game_datetime FROM games g "
        "JOIN team_game_stats t ON t.event_slug=g.event_slug AND t.game_id=g.game_id "
        "WHERE g.event_slug=? AND t.org_id=? AND g.parsed_at IS NOT NULL "
        "ORDER BY g.game_utc DESC",
        (event_slug, org_id),
    ):
        name = _safe(f"review {row['game_id']}", build_review, conn, event_slug,
                     row["game_id"], org_id, nav=nav, reference=reference)
        if name:
            opponent = conn.execute(
                "SELECT opp_org_id FROM team_game_stats "
                "WHERE event_slug=? AND game_id=? AND org_id=?",
                (event_slug, row["game_id"], org_id)).fetchone()["opp_org_id"]
            identity = analysis.team_identity(conn, event_slug, opponent)
            written["reviews"].append(
                {"href": name, "title": f"Review: Ireland v {identity['name']}",
                 "when": _local(row["game_utc"], row["game_datetime"])})

    _safe("index", build_index, conn, event_slug, org_id, written, nav=nav,
          reference=reference)
    return written
