"""Reconstruct which five players were on court, and for how long.

The feed does not state lineups directly, but it does record every substitution
with a timestamp, and it records period-break substitutions explicitly at the
top of each period's event list (clock 10:00, before the STARTP marker). So the
on-court five carries forward across period boundaries and the break subs apply
cleanly at the period start.

Reconstruction is still checked rather than trusted: `validate` compares the
minutes we derive against the official boxscore minutes for every player. A game
that fails is marked unreliable and its lineup sections are suppressed in
reports, because a plausible but wrong lineup number is worse than none.
"""
from __future__ import annotations

from collections import defaultdict

from .config import OT_SECONDS, PERIOD_SECONDS
from .clock import period_index

# A player's derived minutes may differ from the official figure by this much
# before we treat the game's lineup data as unusable.
#
# Measured against all 81 games of the 2025 U16 Division B event (1,928 player
# games): 85.8% matched exactly, 100% fell within 5 seconds, worst case 2
# seconds. 10 seconds therefore sits far above the observed noise floor while
# still tripping immediately if the feed's substitution reporting changes.
TOLERANCE_SECONDS = 10


def game_length_seconds(events: list[dict]) -> int:
    """Regulation plus any overtime actually played."""
    highest = max((period_index(e.get("period")) for e in events), default=3)
    if highest < 4:
        return 4 * PERIOD_SECONDS
    return 4 * PERIOD_SECONDS + (highest - 3) * OT_SECONDS


def build_stints(game: dict, elapsed_of) -> dict:
    """Walk the play-by-play and emit constant-lineup intervals per team.

    `elapsed_of(event)` maps an event to seconds since tip. Returns stints keyed
    by org id, plus the per-player seconds implied by them.
    """
    events = sorted(game["events"], key=lambda e: e["order"] or 0)
    total_seconds = game_length_seconds(events)

    side_of_org = {t["org_id"]: t["side"] for t in game["teams"]}
    org_ids = list(side_of_org)

    on_court = {
        org_id: {p["person_id"] for p in game["players"]
                 if p["org_id"] == org_id and p["starter"]}
        for org_id in org_ids
    }
    for org_id, five in on_court.items():
        if len(five) != 5:
            raise LineupError(f"team {org_id} has {len(five)} starters, expected 5")

    score = {"A": 0, "B": 0}
    stints = {org_id: [] for org_id in org_ids}
    open_at = {org_id: 0 for org_id in org_ids}
    open_score = {org_id: (0, 0) for org_id in org_ids}

    def close(org_id: int, at_seconds: int) -> None:
        start = open_at[org_id]
        duration = at_seconds - start
        if duration > 0:
            side = side_of_org[org_id]
            opp_side = "B" if side == "A" else "A"
            start_for, start_against = open_score[org_id]
            stints[org_id].append(
                {
                    "lineup": tuple(sorted(on_court[org_id])),
                    "start_seconds": start,
                    "end_seconds": at_seconds,
                    "seconds": duration,
                    "points_for": score[side] - start_for,
                    "points_against": score[opp_side] - start_against,
                }
            )
        open_at[org_id] = at_seconds
        side = side_of_org[org_id]
        opp_side = "B" if side == "A" else "A"
        open_score[org_id] = (score[side], score[opp_side])

    for event in events:
        if event.get("score_a") is not None:
            score["A"] = event["score_a"]
        if event.get("score_b") is not None:
            score["B"] = event["score_b"]

        if event.get("action_code") != "SUBST":
            continue

        org_id = event.get("org_id")
        person_id = event.get("person_id")
        if org_id not in on_court or person_id is None:
            continue

        at = elapsed_of(event)
        if at is None:
            continue
        at = max(0, min(at, total_seconds))

        close(org_id, at)
        if event.get("sub_direction") == "IN":
            on_court[org_id].add(person_id)
        elif event.get("sub_direction") == "OUT":
            on_court[org_id].discard(person_id)

    for org_id in org_ids:
        close(org_id, total_seconds)

    player_seconds: dict[int, int] = defaultdict(int)
    for org_stints in stints.values():
        for stint in org_stints:
            for person_id in stint["lineup"]:
                player_seconds[person_id] += stint["seconds"]

    return {
        "stints": stints,
        "player_seconds": dict(player_seconds),
        "total_seconds": total_seconds,
    }


class LineupError(RuntimeError):
    """Reconstruction could not proceed at all."""


def validate(game: dict, result: dict, tolerance: int = TOLERANCE_SECONDS) -> dict:
    """Compare derived minutes against the official boxscore, per player."""
    derived = result["player_seconds"]
    discrepancies = []
    for player in game["players"]:
        official = player.get("seconds_played")
        if official is None:
            continue
        got = derived.get(player["person_id"], 0)
        delta = got - official
        if abs(delta) > tolerance:
            discrepancies.append(
                {
                    "person_id": player["person_id"],
                    "name": player["full_name"],
                    "official": official,
                    "derived": got,
                    "delta": delta,
                }
            )

    all_deltas = [
        abs(derived.get(p["person_id"], 0) - p["seconds_played"])
        for p in game["players"]
        if p.get("seconds_played") is not None
    ]

    # Each team must field exactly five players at every instant.
    size_errors = []
    for org_id, org_stints in result["stints"].items():
        for stint in org_stints:
            if len(stint["lineup"]) != 5:
                size_errors.append(
                    {
                        "org_id": org_id,
                        "start": stint["start_seconds"],
                        "size": len(stint["lineup"]),
                    }
                )

    return {
        "ok": not discrepancies and not size_errors,
        "max_error_seconds": max(all_deltas) if all_deltas else 0,
        "discrepancies": discrepancies,
        "size_errors": size_errors,
    }


def reconstruct(game: dict, elapsed_of) -> tuple[dict, dict]:
    """Build stints and validate them in one step."""
    result = build_stints(game, elapsed_of)
    report = validate(game, result)
    return result, report


def aggregate_lineups(stints_by_org: dict, org_id: int) -> list[dict]:
    """Collapse a team's stints into per-lineup totals across a game or event."""
    totals: dict[tuple, dict] = {}
    for stint in stints_by_org.get(org_id, []):
        entry = totals.setdefault(
            stint["lineup"],
            {"lineup": stint["lineup"], "seconds": 0, "points_for": 0,
             "points_against": 0, "stints": 0},
        )
        entry["seconds"] += stint["seconds"]
        entry["points_for"] += stint["points_for"]
        entry["points_against"] += stint["points_against"]
        entry["stints"] += 1

    out = sorted(totals.values(), key=lambda e: -e["seconds"])
    for entry in out:
        entry["plus_minus"] = entry["points_for"] - entry["points_against"]
        entry["minutes"] = entry["seconds"] / 60.0
    return out
