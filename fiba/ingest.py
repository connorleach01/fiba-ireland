"""Turn a parsed game into database rows."""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from . import db, fetch, lineups, parse
from .clock import clock_to_elapsed, period_index  # noqa: F401

log = logging.getLogger(__name__)

# Venue timezones for the host countries these events use.
_VENUE_TZ = {
    "MKD": "Europe/Skopje",
    "CRO": "Europe/Zagreb",
    "BIH": "Europe/Sarajevo",
    "BUL": "Europe/Sofia",
    "MNE": "Europe/Podgorica",
    "IRL": "Europe/Dublin",
    "GBR": "Europe/London",
    "POR": "Europe/Lisbon",
}
_DEFAULT_TZ = "Europe/Skopje"
IRISH_TZ = ZoneInfo("Europe/Dublin")


def venue_timezone(country_code: str | None) -> ZoneInfo:
    return ZoneInfo(_VENUE_TZ.get((country_code or "").upper(), _DEFAULT_TZ))


def to_utc(local_iso: str | None, country_code: str | None) -> str | None:
    """`gameDateTime` is published as venue-local wall time with no offset."""
    if not local_iso:
        return None
    try:
        naive = dt.datetime.fromisoformat(local_iso)
    except ValueError:
        return None
    aware = naive.replace(tzinfo=venue_timezone(country_code))
    return aware.astimezone(dt.timezone.utc).isoformat()


def store_game(conn, event_slug: str, schedule_row: dict, game: dict) -> None:
    """Persist one parsed game. Caller commits."""
    game_id = schedule_row["game_id"]
    teams = game["teams"]
    by_org = {t["org_id"]: t for t in teams}
    org_ids = list(by_org)
    opponent = {org_ids[0]: org_ids[1], org_ids[1]: org_ids[0]}

    # Player identities.
    db._upsert(
        conn,
        "players",
        [
            {
                "person_id": p["person_id"],
                "full_name": p["full_name"],
                "first_name": p.get("first_name"),
                "last_name": p.get("last_name"),
                "uniform_number": p.get("uniform_number"),
                "position": p.get("position"),
            }
            for p in game["roster"].values()
        ],
        ["person_id"],
    )

    # Team lines.
    team_rows = []
    period_rows = []
    for team in teams:
        org_id = team["org_id"]
        opp_id = opponent[org_id]
        row = {
            "event_slug": event_slug,
            "game_id": game_id,
            "org_id": org_id,
            "opp_org_id": opp_id,
            "side": team["side"],
            "score": team["score"],
            "opp_score": by_org[opp_id]["score"],
            "won": int((team["score"] or 0) > (by_org[opp_id]["score"] or 0)),
            "biggest_lead_score": team.get("biggest_lead_score"),
            "biggest_run_score": team.get("biggest_run_score"),
        }
        for key in parse._TEAM_STAT_KEYS:
            row[key] = team.get(key)
        team_rows.append(row)

        for period in team["periods"]:
            period_rows.append(
                {
                    "event_slug": event_slug,
                    "game_id": game_id,
                    "org_id": org_id,
                    "period": period["period"],
                    "score": period["score"],
                }
            )

    db._upsert(conn, "team_game_stats", team_rows, ["event_slug", "game_id", "org_id"])
    db._upsert(conn, "team_periods", period_rows,
               ["event_slug", "game_id", "org_id", "period"])

    # Player lines.
    player_rows = []
    for player in game["players"]:
        row = {
            "event_slug": event_slug,
            "game_id": game_id,
            "person_id": player["person_id"],
            "org_id": player["org_id"],
            "opp_org_id": opponent[player["org_id"]],
            "side": player["side"],
            "full_name": player["full_name"],
            "uniform_number": player.get("uniform_number"),
            "position": player.get("position"),
            "starter": int(player["starter"]),
            "has_played": int(player["has_played"]),
            "seconds_played": player["seconds_played"],
            "time_played": player["time_played"],
        }
        for key in parse._PLAYER_STAT_KEYS:
            row[key] = player.get(key)
        player_rows.append(row)
    db._upsert(conn, "player_game_stats", player_rows,
               ["event_slug", "game_id", "person_id"])

    # Play by play.
    event_rows = []
    for event in game["events"]:
        event_rows.append(
            {
                "event_slug": event_slug,
                "game_id": game_id,
                "order": event["order"],
                "period": event["period"],
                "event_id": event["event_id"],
                "act": event["act"],
                "action_code": event["action_code"],
                "clock": event["clock"],
                "game_seconds": clock_to_elapsed(event["period"], event["clock"]),
                "score_a": event["score_a"],
                "score_b": event["score_b"],
                "org_id": event["org_id"],
                "person_id": event["person_id"],
                "person_id_2": event["person_id_2"],
                "text": event["text"],
                "sub_direction": event["sub_direction"],
                "made": None if event["made"] is None else int(event["made"]),
                "shot_points": event["shot_points"],
                "x": event["x"],
                "y": event["y"],
            }
        )
    db._upsert(conn, "pbp_events", event_rows, ["event_slug", "game_id", "order"])

    # Lineups, with their validation verdict recorded alongside.
    lineups_ok, max_err = None, None
    try:
        result, report = lineups.reconstruct(
            game, lambda e: clock_to_elapsed(e["period"], e["clock"])
        )
        lineups_ok = int(report["ok"])
        max_err = report["max_error_seconds"]
        if not report["ok"]:
            log.warning(
                "game %s lineup validation failed: max error %ss, %d discrepancies",
                game_id, max_err, len(report["discrepancies"]),
            )
        stint_rows = []
        for org_id, org_stints in result["stints"].items():
            for index, stint in enumerate(org_stints):
                stint_rows.append(
                    {
                        "event_slug": event_slug,
                        "game_id": game_id,
                        "org_id": org_id,
                        "stint_index": index,
                        "lineup_key": ",".join(str(p) for p in stint["lineup"]),
                        "start_seconds": stint["start_seconds"],
                        "end_seconds": stint["end_seconds"],
                        "seconds": stint["seconds"],
                        "points_for": stint["points_for"],
                        "points_against": stint["points_against"],
                    }
                )
        db._upsert(conn, "stints", stint_rows,
                   ["event_slug", "game_id", "org_id", "stint_index"])
    except Exception as exc:  # noqa: BLE001 - box score stays usable without lineups
        lineups_ok = 0
        log.warning("game %s lineup reconstruction failed: %s", game_id, exc)

    conn.execute(
        "UPDATE games SET parsed_at=?, game_utc=COALESCE(?, game_utc), "
        "lineups_ok=?, lineup_max_err=? WHERE event_slug=? AND game_id=?",
        (
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            to_utc(schedule_row.get("game_datetime"), _country_code(schedule_row)),
            lineups_ok,
            max_err,
            event_slug,
            game_id,
        ),
    )


def _country_code(schedule_row: dict) -> str | None:
    """Host country is stored by name; map the ones our events use."""
    names = {
        "north macedonia": "MKD",
        "croatia": "CRO",
        "bosnia and herzegovina": "BIH",
        "bulgaria": "BUL",
        "montenegro": "MNE",
        "ireland": "IRL",
        "portugal": "POR",
    }
    return names.get((schedule_row.get("host_country") or "").strip().lower())


def sync_event(conn, event_slug: str, *, use_cache: bool = True,
               only_new: bool = True) -> dict:
    """Refresh the schedule, then scrape any final game we do not yet hold.

    Returns a summary including the ids of games newly ingested, which is what
    the poller uses to decide which reports to rebuild.
    """
    schedule = parse.parse_schedule(fetch.fetch_schedule_html(event_slug))
    # Resolve tip times up front so upcoming fixtures render in Irish time too,
    # not just games we have already scraped.
    for row in schedule:
        row["game_utc"] = to_utc(row.get("game_datetime"), _country_code(row))
    db.upsert_schedule(conn, event_slug, schedule)

    already = db.scraped_game_ids(conn, event_slug) if only_new else set()
    finals = [g for g in schedule if parse.is_final(g["status_code"])]
    todo = [g for g in finals if g["game_id"] not in already]

    # When polling live, a game we have not ingested is either new or previously
    # failed, so always pull a fresh copy. Reusing the cache there could pin us to
    # a page captured while the box score was still incomplete, and no amount of
    # retrying would ever get past it. Backfills read the cache as normal.
    fetch_from_cache = use_cache and not only_new

    ingested, failed = [], []
    for row in todo:
        try:
            html = fetch.fetch_game_html(
                event_slug, row["game_id"], row["team_a_code"], row["team_b_code"],
                use_cache=fetch_from_cache,
            )
            game = parse.parse_game(html)
            store_game(conn, event_slug, row, game)
            conn.commit()
            ingested.append(row["game_id"])
            log.info("ingested %s %s v %s", row["game_id"], row["team_a_code"],
                     row["team_b_code"])
        except Exception as exc:  # noqa: BLE001 - one bad game must not stop the rest
            conn.rollback()
            # Drop the cache entry so a truncated or half-published page cannot
            # wedge this game permanently. The next poll starts clean.
            fetch.discard_cached_game(event_slug, row["game_id"])
            failed.append((row["game_id"], str(exc)))
            log.error("failed %s %s v %s: %s", row["game_id"], row["team_a_code"],
                      row["team_b_code"], exc)

    return {
        "event_slug": event_slug,
        "scheduled": len(schedule),
        "final": len(finals),
        "ingested": ingested,
        "failed": failed,
    }
