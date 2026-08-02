"""Extraction of the LiveStats feed FIBA embeds in its server-rendered pages.

FIBA's site is a React Server Components app. The whole payload arrives inside
`self.__next_f.push([1,"<chunk>"])` script tags: each chunk is a JavaScript
string literal holding a slice of one long "flight" string. Reassembling those
chunks in document order and JSON-decoding each one gives us a single text blob
containing the complete game feed.

We then pull objects out of that blob by anchoring on a known key and walking
balanced braces, rather than with one large regex. Anchors survive cosmetic
changes to the site; a mega-regex does not.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Matches the string literal argument of self.__next_f.push([1,"..."]).
_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\s*\]\)', re.DOTALL)

# React marks absent values with this sentinel rather than null.
UNDEFINED = "$undefined"


class ParseError(RuntimeError):
    """Raised when the page does not contain the data we expect."""


# --------------------------------------------------------------------------
# Flight payload plumbing
# --------------------------------------------------------------------------


def flight_payload(html: str) -> str:
    """Reassemble the RSC flight string from its script chunks."""
    chunks = _CHUNK_RE.findall(html)
    if not chunks:
        raise ParseError("no __next_f chunks found; page shape has changed")
    parts = []
    for literal in chunks:
        try:
            parts.append(json.loads(literal))
        except json.JSONDecodeError:
            # A malformed chunk is survivable as long as the rest decode.
            continue
    if not parts:
        raise ParseError("no __next_f chunk could be decoded")
    return "".join(parts)


def extract_json_at(text: str, start: int) -> Any:
    """Decode the JSON value beginning at `start`, respecting string literals.

    The flight blob is not valid JSON as a whole, so json.JSONDecoder.raw_decode
    is pointed at the exact offset where a value begins.
    """
    if start < 0 or start >= len(text):
        raise ParseError(f"offset {start} outside payload")
    if text[start] not in "[{":
        raise ParseError(f"expected an object or array at offset {start}")
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ParseError(f"could not decode JSON at offset {start}: {exc}") from exc
    return value


def _iter_values(text: str, key: str):
    """Yield every decodable value stored under `"key":`.

    The payload also carries an i18n dictionary, so a key like "games" appears
    both as a label ("Game(s)") and as the real data. Callers pick the match
    that has the shape they need rather than assuming the first hit is right.
    """
    anchor = f'"{key}":'
    start = 0
    while True:
        index = text.find(anchor, start)
        if index < 0:
            return
        start = index + len(anchor)
        if start < len(text) and text[start] in "[{":
            try:
                yield extract_json_at(text, start)
            except ParseError:
                continue


def _find_value(text: str, key: str, predicate=None) -> Any:
    """First value under `"key":` that satisfies `predicate`."""
    for value in _iter_values(text, key):
        if predicate is None or predicate(value):
            return value
    raise ParseError(f'no usable value found for key "{key}"')


def _clean(value: Any) -> Any:
    """Turn React's $undefined sentinel into None, recursively."""
    if value == UNDEFINED:
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------


def parse_schedule(html: str) -> list[dict]:
    """Every game in the event, from the event's games page.

    `status_code` is the field that matters operationally: it flips from INIT to
    VALID when a game is final, which is our trigger to scrape.
    """
    payload = flight_payload(html)
    games = _find_value(
        payload,
        "games",
        lambda v: isinstance(v, list) and v and isinstance(v[0], dict) and "gameId" in v[0],
    )

    out = []
    for game in games:
        game = _clean(game)
        team_a = game.get("teamA") or {}
        team_b = game.get("teamB") or {}
        out.append(
            {
                "game_id": game["gameId"],
                "game_number": game.get("gameNumber"),
                "game_name": game.get("gameName"),
                "status_code": game.get("statusCode"),
                "is_live": bool(game.get("isLive")),
                "team_a_org_id": team_a.get("organisationId"),
                "team_a_code": team_a.get("code"),
                "team_a_name": team_a.get("officialName") or team_a.get("shortName"),
                "team_b_org_id": team_b.get("organisationId"),
                "team_b_code": team_b.get("code"),
                "team_b_name": team_b.get("officialName") or team_b.get("shortName"),
                "team_a_score": game.get("teamAScore"),
                "team_b_score": game.get("teamBScore"),
                "game_datetime": game.get("gameDateTime"),
                "host_city": game.get("hostCity"),
                "host_country": game.get("hostCountry"),
                "venue_name": game.get("venueName"),
            }
        )
    return out


def is_final(status_code: str | None) -> bool:
    """FIBA marks finished games VALID; INIT means not yet played."""
    return (status_code or "").upper() in {"VALID", "COMPLETE", "COMPLETED", "FINISHED"}


# --------------------------------------------------------------------------
# Game page
# --------------------------------------------------------------------------


def _parse_rosters(payload: str) -> dict[int, dict]:
    """personId -> player identity, merged across both teams."""
    roster: dict[int, dict] = {}
    for key in ("playersTeamA", "playersTeamB"):
        try:
            players = _find_value(
                payload,
                key,
                lambda v: isinstance(v, list) and v and isinstance(v[0], dict)
                and "personId" in v[0],
            )
        except ParseError:
            continue
        for player in _clean(players) or []:
            person_id = player.get("personId")
            if person_id is None:
                continue
            first = (player.get("firstName") or "").strip()
            last = (player.get("lastName") or "").strip()
            roster[int(person_id)] = {
                "person_id": int(person_id),
                "first_name": first,
                "last_name": last,
                "full_name": f"{first} {last}".strip(),
                "uniform_number": player.get("uniformNumber"),
                "position": player.get("position"),
                "is_captain": bool(player.get("isCaptain")),
                "roster_side": "A" if key == "playersTeamA" else "B",
            }
    if not roster:
        raise ParseError("no roster found on game page")
    return roster


def _parse_team_blocks(payload: str) -> list[dict]:
    """The two `{"Id":"T_<orgId>", ...}` objects holding team and player stats."""
    blocks = []
    seen_org_ids = set()
    for match in re.finditer(r'\{"Id":"T_(\d+)"', payload):
        org_id = int(match.group(1))
        if org_id in seen_org_ids:
            continue
        try:
            block = _clean(extract_json_at(payload, match.start()))
        except ParseError:
            continue
        # The real block carries players and stats; navigation stubs do not.
        if not isinstance(block, dict) or "Children" not in block or "Stats" not in block:
            continue
        seen_org_ids.add(org_id)
        block["_org_id"] = org_id
        blocks.append(block)

    if len(blocks) != 2:
        raise ParseError(f"expected 2 team stat blocks, found {len(blocks)}")
    return blocks


def _parse_pbp(payload: str) -> list[dict]:
    """Play-by-play, which the feed nests one array per period.

    Shape: `"playByPlay":{"items":{"Q1":{"name","scoreA","scoreB","items":[...]},
    "Q2":{...}, ...}}`. Periods are flattened into a single ordered stream while
    keeping the period label on each event, which lineup reconstruction needs.
    """
    play_by_play = _find_value(
        payload,
        "playByPlay",
        lambda v: isinstance(v, dict) and isinstance(v.get("items"), dict),
    )
    periods = play_by_play["items"]

    out = []
    for period_key, period in periods.items():
        if not isinstance(period, dict):
            continue
        label = period.get("name") or period_key
        for event in _clean(period.get("items") or []):
            out.append(
                {
                    "period": label,
                    "event_id": _to_int(event.get("Id")),
                    "order": _to_int(event.get("order")),
                    "act": event.get("act"),
                    "action_code": event.get("ac"),
                    "clock": event.get("Time"),
                    "wall_clock_ms": event.get("GT"),
                    "score_a": event.get("SA"),
                    "score_b": event.get("SB"),
                    "org_id": event.get("oId"),
                    "person_id": event.get("pId"),
                    "person_id_2": event.get("p2Id"),
                    "text": event.get("txt") or "",
                    "sub_direction": event.get("in"),
                    "made": event.get("made"),
                    "shot_points": event.get("pts"),
                    "x": event.get("x"),
                    "y": event.get("y"),
                }
            )

    if not out:
        raise ParseError("play-by-play held no events")

    out.sort(key=lambda e: (e["order"] if e["order"] is not None else 0))
    return out


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _minutes_to_seconds(text: str | None) -> int | None:
    """Boxscore minutes arrive as MM:SS."""
    if not text or ":" not in text:
        return None
    minutes, _, seconds = text.partition(":")
    try:
        return int(minutes) * 60 + int(seconds)
    except ValueError:
        return None


# Team-level keys that are counting stats rather than nested structures.
_TEAM_STAT_KEYS = (
    "PTS FGM FGA FGP FG2M FG2A FG2P FG3M FG3A FG3P FTM FTA FTP "
    "OR DR REB AS ST BS TO PF FD EFF "
    "T_OR T_DR T_REB T_PF T_TO "
    "A_PIP A_SCP A_FBP A_PAT A_PFB A_BL A_BR"
).split()

_PLAYER_STAT_KEYS = (
    "PTS FGM FGA FGP FG2M FG2A FG2P FG3M FG3A FG3P FTM FTA FTP "
    "OR DR REB AS ST BS BSR TO PF FD PM EFF"
).split()


def parse_game(html: str) -> dict:
    """Parse a full game page into rosters, team stats, player stats and PBP."""
    payload = flight_payload(html)

    roster = _parse_rosters(payload)
    team_blocks = _parse_team_blocks(payload)
    events = _parse_pbp(payload)

    teams = []
    players = []
    for side, block in zip(("A", "B"), team_blocks):
        org_id = block["_org_id"]
        stats = block.get("Stats") or {}
        periods = []
        for period in block.get("Periods") or []:
            periods.append(
                {
                    "period": period.get("Id"),
                    "score": period.get("Score"),
                    "half_time_score": period.get("HalfTimeScore"),
                }
            )

        team = {
            "org_id": org_id,
            "side": side,
            "score": block.get("Score"),
            "periods": periods,
            "biggest_lead_score": stats.get("A_BLS"),
            "biggest_run_score": stats.get("A_BRS"),
        }
        for key in _TEAM_STAT_KEYS:
            team[key] = stats.get(key)
        teams.append(team)

        for child in block.get("Children") or []:
            raw_id = str(child.get("Id") or "")
            if not raw_id.startswith("P_"):
                continue
            person_id = _to_int(raw_id[2:])
            if person_id is None:
                continue
            child_stats = child.get("Stats") or {}
            identity = roster.get(person_id, {})
            player = {
                "person_id": person_id,
                "org_id": org_id,
                "side": side,
                "full_name": identity.get("full_name") or f"#{person_id}",
                "uniform_number": identity.get("uniform_number"),
                "position": identity.get("position"),
                "is_captain": identity.get("is_captain", False),
                "starter": bool(child_stats.get("Starter")),
                "has_played": bool(child_stats.get("HasPlayed")),
                "seconds_played": _minutes_to_seconds(child_stats.get("TP")),
                "time_played": child_stats.get("TP"),
            }
            for key in _PLAYER_STAT_KEYS:
                player[key] = child_stats.get(key)
            players.append(player)

    if not players:
        raise ParseError("no player rows parsed")

    game = {
        "teams": teams,
        "players": players,
        "events": events,
        "roster": roster,
    }
    validate_game(game)
    return game


def validate_game(game: dict) -> None:
    """Fail loudly rather than emit a half-parsed game.

    A quietly wrong boxscore is far more damaging to a coaching staff than a
    scrape that refuses to run, so every invariant here raises.
    """
    teams = game["teams"]
    if len(teams) != 2:
        raise ParseError(f"expected 2 teams, got {len(teams)}")

    for team in teams:
        org_id = team["org_id"]
        roster_points = sum(
            (p["PTS"] or 0) for p in game["players"] if p["org_id"] == org_id
        )
        team_points = team.get("PTS")
        if team_points is None:
            raise ParseError(f"team {org_id} has no PTS total")
        if roster_points != team_points:
            raise ParseError(
                f"team {org_id}: player points sum to {roster_points} "
                f"but team total is {team_points}"
            )
        if team.get("score") is not None and team["score"] != team_points:
            raise ParseError(
                f"team {org_id}: box score total {team_points} "
                f"disagrees with final score {team['score']}"
            )

        # Shooting splits must be internally consistent.
        made, attempted = team.get("FGM"), team.get("FGA")
        two_m, three_m = team.get("FG2M"), team.get("FG3M")
        if None not in (made, two_m, three_m) and two_m + three_m != made:
            raise ParseError(
                f"team {org_id}: FG2M+FG3M={two_m + three_m} does not equal FGM={made}"
            )
        if None not in (made, attempted) and made > attempted:
            raise ParseError(f"team {org_id}: FGM {made} exceeds FGA {attempted}")

        # Points must reconcile with the shooting lines.
        derived = 2 * (two_m or 0) + 3 * (three_m or 0) + (team.get("FTM") or 0)
        if derived != team_points:
            raise ParseError(
                f"team {org_id}: shooting implies {derived} points "
                f"but total is {team_points}"
            )

    known_ids = set(game["roster"])
    unknown = {
        e["person_id"]
        for e in game["events"]
        if e["person_id"] is not None and e["person_id"] not in known_ids
    }
    if unknown:
        raise ParseError(f"play-by-play references players missing from roster: {sorted(unknown)}")

    org_ids = {t["org_id"] for t in teams}
    if len(org_ids) != 2:
        raise ParseError(f"both team blocks share org id {org_ids}")
