"""Regression tests over cached game pages. No network required.

    python -m tests.test_pipeline
"""
from __future__ import annotations

import sys

from fiba import analysis, clock, db, fetch, lineups, metrics, parse, theming
from fiba.config import IRELAND_ORG_ID

U16 = "fiba-u16-eurobasket-2025-division-b"
U18 = "fiba-u18-eurobasket-2026-division-b"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  pass  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        _failures.append(name)


def test_clock():
    print("clock")
    check("Q1 tip is zero", clock.clock_to_elapsed("Q1", "10:00") == 0)
    check("Q1 end is 600s", clock.clock_to_elapsed("Q1", "0:00") == 600)
    check("Q3 midpoint", clock.clock_to_elapsed("Q3", "5:00") == 1500)
    check("Q4 end is 2400s", clock.clock_to_elapsed("Q4", "0:00") == 2400)
    check("OT1 continues past regulation",
          clock.clock_to_elapsed("OT1", "5:00") == 2400)
    check("bad clock returns None", clock.clock_to_elapsed("Q1", "") is None)


def test_parse_both_encodings():
    """One parser must handle both the inline and the reference-encoded payload."""
    print("parser, both events")
    for event, game_id in ((U16, 126011), (U18, 132202)):
        html = fetch.cached_game_html(event, game_id)
        if html is None:
            check(f"{event} {game_id} cached", False, "(no cached page)")
            continue
        game = parse.parse_game(html)
        check(f"{event} {game_id} two teams", len(game["teams"]) == 2)
        check(f"{event} {game_id} has players", len(game["players"]) >= 20)
        check(f"{event} {game_id} has play-by-play", len(game["events"]) > 300)
        check(f"{event} {game_id} shots carry coordinates",
              any(e["x"] is not None for e in game["events"] if e["act"] == "shot"))


def test_metrics_match_published():
    """Our percentages must agree with FIBA's own, allowing for rounding."""
    print("metrics vs published percentages")
    conn = db.connect()
    worst = 0.0
    rows = conn.execute("SELECT * FROM team_game_stats").fetchall()
    for row in rows:
        for made, att, published in (("FGM", "FGA", "FGP"),
                                     ("FG3M", "FG3A", "FG3P"),
                                     ("FTM", "FTA", "FTP")):
            if row[att]:
                ours = 100.0 * row[made] / row[att]
                worst = max(worst, abs(ours - (row[published] or 0)))
    check(f"within 0.1pp across {len(rows)} team-games", worst < 0.1,
          f"(worst {worst:.3f})")


def test_four_factors_sanity():
    print("four factors")
    team = {"FGA": 60, "FGM": 24, "FG3M": 6, "FTA": 20, "FTM": 12, "OR": 10,
            "DR": 20, "TO": 15, "PTS": 66, "REB": 30, "FG2A": 40}
    opp = {"FGA": 60, "FGM": 24, "FG3M": 6, "FTA": 20, "FTM": 12, "OR": 10,
           "DR": 20, "TO": 15, "PTS": 66, "REB": 30, "FG2A": 40}
    ff = metrics.four_factors(team, opp)
    check("eFG% = (FGM + 0.5*3PM)/FGA", abs(ff["efg_pct"] - 45.0) < 1e-9)
    check("OREB% uses opponent DREB", abs(ff["oreb_pct"] - 100 * 10 / 30) < 1e-9)
    check("FTA/FGA rate", abs(ff["ft_rate"] - 100 * 20 / 60) < 1e-9)
    check("possessions", abs(ff["possessions"] - (60 + 8.8 - 10 + 15)) < 1e-9)
    check("net rating is zero for identical teams", abs(ff["net_rating"]) < 1e-9)


def test_shot_zones():
    print("shot zones")
    from fiba.config import BASKET_X, BASKET_Y
    at_rim = {"action_code": "P2", "x": BASKET_X, "y": BASKET_Y, "text": "layup"}
    check("basket location classifies as rim",
          metrics.classify_zone(at_rim) == "Rim")
    corner = {"action_code": "P3", "x": 20, "y": BASKET_Y, "text": "3pt"}
    check("baseline three is a corner three",
          metrics.classify_zone(corner) == "Corner 3")
    top = {"action_code": "P3", "x": BASKET_X, "y": 170, "text": "3pt from center"}
    check("straightaway three is a top three",
          metrics.classify_zone(top) == "Top 3")
    check("free throws have no zone",
          metrics.classify_zone({"action_code": "FT", "x": 0, "y": 0}) is None)


def test_rejects_unfinished_games():
    """A game caught mid-play must never be stored as final.

    Every other invariant holds just as well at half time, because a live box
    score is internally consistent; it is only incomplete. Since a stored game
    is never revisited, the period scores are the thing that has to catch this.
    """
    print("unfinished games are refused")
    import copy
    from fiba import fetch

    html = fetch.fetch_game_html(
        "fiba-u18-eurobasket-2026-division-b", 132202, "POR", "IRL", use_cache=True)
    game = parse.parse_game(html)

    def rejected(mutate) -> bool:
        candidate = copy.deepcopy(game)
        mutate(candidate)
        try:
            parse.validate_game(candidate)
        except parse.ParseError:
            return True
        return False

    def truncate(n):
        def apply(g):
            for team in g["teams"]:
                team["periods"] = team["periods"][:n]
        return apply

    check("a game caught at half time is refused", rejected(truncate(2)))
    check("a game caught in the fourth is refused", rejected(truncate(3)))
    check("a game whose last period is unscored is refused",
          rejected(lambda g: g["teams"][0]["periods"][3].update(score=0)))
    check("the finished game itself still passes",
          not rejected(lambda g: None))

    # The poller must also refuse anything the schedule flags as in progress,
    # whatever its status code says.
    check("a live game is not treated as final",
          parse.is_final("VALID") and not parse.is_final("LIVE")
          and not parse.is_final("INIT"))


def test_lineups_validate():
    """Derived minutes must reconcile with the official boxscore."""
    print("lineup reconstruction")
    conn = db.connect()
    total = ok = 0
    worst = 0
    for event in (U16, U18):
        for row in conn.execute(
            "SELECT game_id, lineup_max_err, lineups_ok FROM games "
            "WHERE event_slug=? AND parsed_at IS NOT NULL", (event,)
        ):
            total += 1
            ok += 1 if row["lineups_ok"] else 0
            worst = max(worst, row["lineup_max_err"] or 0)
    check(f"all {total} games validate", total > 0 and ok == total,
          f"({ok}/{total} ok)")
    check("worst minute error within tolerance",
          worst <= lineups.TOLERANCE_SECONDS, f"(worst {worst}s)")


def test_small_sample_rates_withheld():
    """A rate must not be published off a sample too small to support it."""
    print("small-sample guards")
    check("threshold is at least two minutes", analysis.MIN_RATE_SECONDS >= 120)
    conn = db.connect()
    rows = conn.execute(
        "SELECT s.seconds FROM stints s WHERE s.seconds > 0 LIMIT 1").fetchall()
    check("stints are stored", len(rows) > 0)


def test_fixture_list():
    """The schedule page has to be complete and honest before a ball is thrown."""
    print("fixture list")
    conn = db.connect()

    upcoming = analysis.event_fixtures(conn, "fiba-u16-eurobasket-2026-division-b")
    check("every fixture is listed before the event starts", len(upcoming) == 81)
    check("nothing is marked played yet",
          not any(f["played"] for f in upcoming))
    # An unplayed game reports 0-0 in the feed, which must never reach a page.
    check("an unplayed game shows no score",
          all(f["home"]["score"] is None and f["away"]["score"] is None
              for f in upcoming))
    check("fixtures are in tip order",
          [f["game_utc"] for f in upcoming] == sorted(f["game_utc"] for f in upcoming))
    # Knockout ties are published before the bracket resolves, with no teams. The
    # page must say so rather than render blank rows, and the group stage, which
    # is what matters on day one, must be fully assigned.
    named = [f for f in upcoming if f["home"]["code"] and f["away"]["code"]]
    check("the group stage is fully assigned", len(named) == 50,
          f"(got {len(named)})")
    check("Ireland's group games all name an opponent",
          all(f["home"]["code"] and f["away"]["code"] for f in upcoming
              if IRELAND_ORG_ID in (f["home"]["org_id"], f["away"]["org_id"])))

    # The U18 event has finished, so every one of its fixtures carries a result.
    # Asserted as "all of them" rather than a count, because the count moved from
    # 78 to 81 when the last three games went final and FIBA published them.
    played = analysis.event_fixtures(conn, U18)
    done = [f for f in played if f["played"]]
    check("a finished event carries every result",
          done and len(done) == len(played), f"({len(done)}/{len(played)})")
    check("a finished game has both scores",
          all(f["home"]["score"] is not None and f["away"]["score"] is not None
              for f in done))
    # Scores come off the schedule feed, so they must agree with the box score
    # the game sheet is built from.
    mismatched = []
    for f in done:
        row = conn.execute(
            "SELECT score FROM team_game_stats WHERE event_slug=? AND game_id=? "
            "AND org_id=?", (U18, f["game_id"], f["home"]["org_id"])).fetchone()
        if row and row["score"] != f["home"]["score"]:
            mismatched.append(f["game_id"])
    check("schedule scores agree with the box score", not mismatched,
          f"(mismatched {mismatched[:5]})")


def test_times_are_venue_local():
    """Every published time must be the wall time at the venue.

    `gameDateTime` arrives as venue-local wall time with no offset and is stored
    as UTC. Rendering that UTC back in the venue's zone has to return the exact
    string FIBA published, which catches an offset applied twice, not at all, or
    in the wrong direction.
    """
    print("times are venue local")
    import datetime as dt
    from fiba import report

    conn = db.connect()
    for slug, expected_zone in (("fiba-u16-eurobasket-2026-division-b", "Europe/Skopje"),
                                (U18, "Europe/Zagreb")):
        zone, country = report._venue(conn, slug)
        check(f"{slug.split('-')[1]} resolves to {expected_zone}",
              str(zone) == expected_zone, f"(got {zone}, host {country})")

        mismatched = 0
        total = 0
        for row in conn.execute(
            "SELECT game_utc, game_datetime FROM games WHERE event_slug=? "
            "AND game_utc IS NOT NULL AND game_datetime IS NOT NULL", (slug,)
        ):
            total += 1
            rendered = dt.datetime.fromisoformat(row["game_utc"]).astimezone(zone)
            if rendered.replace(tzinfo=None) != dt.datetime.fromisoformat(
                    row["game_datetime"]):
                mismatched += 1
        check(f"all {total} tip times round-trip to the feed's wall time",
              total > 0 and mismatched == 0, f"({mismatched} wrong)")

    # Nothing may render in Irish time any more, which was the old behaviour.
    zone, _ = report._venue(conn, "fiba-u16-eurobasket-2026-division-b")
    tip = report._local(zone, "2026-08-06T09:00:00+00:00")
    check("Ireland's opener reads 11:00 at the venue", tip.endswith("11:00"),
          f"(got {tip})")


def test_empty_event_shape():
    """Reports must render before a single game is played."""
    print("pre-tournament shape")
    conn = db.connect()
    league = analysis.event_averages(conn, "does-not-exist")
    check("event averages keep their keys when empty",
          "points_pg" in league and "pace" in league and league["pace"] is None)


def test_theming():
    """Team colours must be readable on paper and separable from each other."""
    print("theming")
    weak = [c for c, v in theming.COUNTRY_INK.items()
            if theming.contrast_ratio(v["primary"]) < theming.MIN_CONTRAST]
    check("every country ink clears 3:1 on white", not weak, f"({weak[:4]})")

    codes = ["IRL", "NED", "MNE", "POR", "SUI", "CRO", "CYP", "MKD", "ISL", "UKR",
             "LUX", "SWE", "BIH", "NOR", "BUL", "FIN", "AUT", "GBR", "EST", "HUN"]
    bad = []
    for a in codes:
        for b in codes:
            if a == b:
                continue
            ink_a, ink_b, _ = theming.pick_pair(a, b)
            if not theming.separable(ink_a, ink_b):
                bad.append((a, b))
    check(f"all {len(codes) * (len(codes) - 1)} matchups separate", not bad,
          f"({bad[:3]})")

    # A lightness-only difference must not count as separation: two adjacent
    # filled bars of the same hue read as one colour however far apart their
    # total distance is.
    green = "#16794a"
    darker = theming.shift_lightness(green, -0.18)
    check("same-hue lightness shift is rejected",
          not theming.separable(green, darker),
          f"(hue distance {theming.chroma_distance(green, darker):.1f})")

    check("Ireland v Portugal avoids green on green",
          theming.chroma_distance(*theming.pick_pair("IRL", "POR")[:2]) >= theming.MIN_DELTA_CHROMA)
    check("unknown country still yields a flag badge",
          "svg" in theming.flag_svg("ZZZ"))
    check("known country renders its flag", "svg" in theming.flag_svg("IRL"))


def test_ranks_and_percentiles():
    """Ranks must respect direction, tie fairly, and skip style metrics."""
    print("ranks and percentiles")
    conn = db.connect()
    ranks = analysis.event_ranks(conn, U18)
    metrics_values = analysis.team_metrics(conn, U18)
    check("every playing team is ranked", len(ranks) == len(metrics_values))

    # Best defensive rating is the lowest number, not the highest.
    best = min(metrics_values.items(), key=lambda kv: kv[1]["def_rating"])[0]
    check("lower defensive rating ranks first",
          ranks[best]["def_rating"]["rank"] == 1,
          f"(got {ranks[best]['def_rating']['rank']})")

    # Highest turnover rate must rank last, since low is better.
    worst = max(metrics_values.items(), key=lambda kv: kv[1]["tov_pct"])[0]
    check("higher turnover rate ranks last",
          ranks[worst]["tov_pct"]["rank"] == len(metrics_values))

    # Style metrics get a rank but no shading tier.
    check("pace carries no tier", ranks[best]["pace"]["tier"] is None)
    check("three-point share carries no tier",
          ranks[best]["three_share"]["tier"] is None)
    check("net rating carries a tier", ranks[best]["net_rating"]["tier"] in (1, 2, 3, 4, 5))
    check("three-point rate is ranked", "fg3_rate" in ranks[best])

    # Every column the leaderboard offers must resolve, or a whole view renders
    # empty. This is exactly what a stale view- class used to do at runtime.
    declared = [m for group in analysis.LEADERBOARD_GROUPS for m in group["metrics"]]
    check("every leaderboard column is declared",
          all(m in analysis.TEAM_METRICS for m in declared))
    check("every leaderboard column has a value",
          all(m in metrics_values[best] for m in declared),
          f"(missing {[m for m in declared if m not in metrics_values[best]]})")

    # Box score and Shooting must mirror themselves: anything shown for a team's
    # own play has to be shown for what it concedes, or a reader comparing the
    # two halves silently compares different things. Advanced is exempt because
    # its defensive metrics are not opp_-prefixed (tov_forced_pct, dreb_pct),
    # and Scoring because bench points have no conceded counterpart in the feed.
    for key in ("box", "shot"):
        view = next(g["metrics"] for g in analysis.LEADERBOARD_GROUPS
                    if g["key"] == key)
        own = [m for m in view if not m.startswith("opp_")]
        conceded = [m[4:] for m in view if m.startswith("opp_")]
        check(f"the {key} view is symmetric", own == conceded,
              f"(own only {sorted(set(own) - set(conceded))}, "
              f"conceded only {sorted(set(conceded) - set(own))})")

    # Conceded counting stats are not simply the opposite of our own: forcing
    # turnovers is good, conceding steals is not.
    check("conceded directions follow meaning, not sign",
          analysis.TEAM_METRICS["opp_tov"]["better"] is True
          and analysis.TEAM_METRICS["opp_stl"]["better"] is False
          and analysis.TEAM_METRICS["opp_pf"]["better"] is True
          and analysis.TEAM_METRICS["opp_ft_pct"]["better"] is None)

    # A zone table looks its ranks up by the slug stamped on each zone row, so
    # every one of those slugs has to be a declared metric on both sides.
    zone_rows = metrics.zone_breakdown(analysis.shots(conn, U18, best))
    slugs = [z[k] for z in zone_rows
             for k in ("metric_share", "metric_fg", "metric_ppa")]
    check("every shot zone is rankable both ways",
          all(s in analysis.TEAM_METRICS and "opp_" + s in analysis.TEAM_METRICS
              for s in slugs),
          f"(missing {[s for s in slugs if s not in analysis.TEAM_METRICS]})")
    check("zone ranks are populated",
          all(s in ranks[best] for s in slugs),
          f"(missing {[s for s in slugs if s not in ranks[best]]})")

    # Points per attempt has an unambiguous direction everywhere, so every zone
    # row must end in a shaded cell whichever side of the ball it describes.
    ppa = [z["metric_ppa"] for z in zone_rows]
    check("every zone shades its points per attempt",
          all(ranks[best][s]["tier"] is not None for s in ppa)
          and all(ranks[best]["opp_" + s]["tier"] is not None for s in ppa))
    check("scoring more per attempt ranks better",
          analysis.TEAM_METRICS["rim_ppa"]["better"] is True
          and analysis.TEAM_METRICS["opp_rim_ppa"]["better"] is False)

    # No shot volume is shaded, on either side of the ball. Where a team shoots
    # from is a style; the accuracy columns beside it carry the judgement.
    shares = [z["metric_share"] for z in zone_rows] + ["three_share"]
    check("no shot share is shaded",
          all(analysis.TEAM_METRICS[s]["better"] is None for s in shares)
          and all(analysis.TEAM_METRICS["opp_" + s]["better"] is None
                  for s in shares),
          f"(shaded {[s for s in shares if analysis.TEAM_METRICS[s]['better'] is not None]})")
    check("shot shares still carry a rank",
          all(ranks[best][s]["rank"] >= 1 for s in shares))

    # The scoring breakdown ranks both a team's own line and what it allows.
    scoring = ["pip", "fbp", "scp", "pat", "bench"]
    check("scoring categories are ranked", all(s in ranks[best] for s in scoring))
    check("conceded scoring categories are ranked",
          all("opp_" + s in ranks[best] for s in scoring if s != "bench"))

    pcts = analysis.player_percentiles(conn, U18)
    pool = pcts["_pool"]
    everyone = [p for team in analysis.event_teams(conn, U18)
                for p in analysis.player_profile(conn, U18, team["org_id"])]
    check("every player who has played is in the pool", pool["size"] == len(everyone))

    # No row of a player table may come up blank: everyone gets ranked on the
    # counting stats, whatever their minutes.
    always_ranked = ("minutes_pg", "pts_pg", "reb_pg", "ast_pg", "usage_pct",
                     "treb_pct", "ast_pct", "tov_pct", "fouls_drawn_per40")
    unranked = [(p["full_name"], key) for p in everyone for key in always_ranked
                if p.get(key) is not None and key not in pcts[p["person_id"]]]
    check("every player is ranked on every counting stat", not unranked,
          f"(missing {unranked[:3]})")

    # Shooting percentages are the exception, and the gate has to actually bite
    # in both directions.
    thin = [p for p in everyone if not analysis.ranks_shooting(p, "fg_pct")]
    check("low-volume shooters exist to be gated", len(thin) > 0)
    check("low-volume shooters carry no FG% rank",
          all("fg_pct" not in pcts[p["person_id"]] for p in thin))
    heavy = [p for p in everyone
             if analysis.ranks_shooting(p, "fg_pct") and p.get("fg_pct") is not None]
    check("volume shooters do carry an FG% rank",
          all("fg_pct" in pcts[p["person_id"]] for p in heavy))


def main() -> int:
    for test in (test_clock, test_parse_both_encodings, test_metrics_match_published,
                 test_four_factors_sanity, test_shot_zones,
                 test_rejects_unfinished_games, test_lineups_validate,
                 test_small_sample_rates_withheld, test_fixture_list,
                 test_times_are_venue_local, test_empty_event_shape,
                 test_theming, test_ranks_and_percentiles):
        test()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
