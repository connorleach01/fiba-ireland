"""SQLite storage for parsed games.

Everything is keyed by (event_slug, game_id) so several events can share one
database. That matters because the pipeline is validated against past events
before the live one starts.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Iterable

from .config import DB_PATH

log = logging.getLogger(__name__)
from .parse import _PLAYER_STAT_KEYS, _TEAM_STAT_KEYS

# Only these hold percentages. Everything else is a count, and must keep INTEGER
# affinity so a report never renders "40.0 points in the paint".
_PERCENT_KEYS = {"FGP", "FG2P", "FG3P", "FTP"}


def _col(key: str) -> str:
    kind = "REAL" if key in _PERCENT_KEYS else "INTEGER"
    return f'"{key}" {kind}'

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS games (
    event_slug      TEXT NOT NULL,
    game_id         INTEGER NOT NULL,
    game_number     TEXT,
    game_name       TEXT,
    status_code     TEXT,
    is_live         INTEGER,
    team_a_org_id   INTEGER,
    team_a_code     TEXT,
    team_a_name     TEXT,
    team_b_org_id   INTEGER,
    team_b_code     TEXT,
    team_b_name     TEXT,
    team_a_score    INTEGER,
    team_b_score    INTEGER,
    game_datetime   TEXT,          -- venue local time, as published
    game_utc        TEXT,          -- resolved UTC
    host_city       TEXT,
    host_country    TEXT,
    venue_name      TEXT,
    parsed_at       TEXT,          -- NULL until the game page is scraped
    lineups_ok      INTEGER,       -- 1 reliable, 0 failed validation, NULL unknown
    lineup_max_err  INTEGER,       -- worst per-player minute error, seconds
    PRIMARY KEY (event_slug, game_id)
);

CREATE TABLE IF NOT EXISTS players (
    person_id       INTEGER PRIMARY KEY,
    full_name       TEXT,
    first_name      TEXT,
    last_name       TEXT,
    uniform_number  TEXT,
    position        TEXT
);

CREATE TABLE IF NOT EXISTS team_game_stats (
    event_slug  TEXT NOT NULL,
    game_id     INTEGER NOT NULL,
    org_id      INTEGER NOT NULL,
    opp_org_id  INTEGER,
    side        TEXT,
    score       INTEGER,
    opp_score   INTEGER,
    won         INTEGER,
    biggest_lead_score TEXT,
    biggest_run_score  TEXT,
    {", ".join(_col(k) for k in _TEAM_STAT_KEYS)},
    PRIMARY KEY (event_slug, game_id, org_id)
);

CREATE TABLE IF NOT EXISTS player_game_stats (
    event_slug      TEXT NOT NULL,
    game_id         INTEGER NOT NULL,
    person_id       INTEGER NOT NULL,
    org_id          INTEGER,
    opp_org_id      INTEGER,
    side            TEXT,
    full_name       TEXT,
    uniform_number  TEXT,
    position        TEXT,
    starter         INTEGER,
    has_played      INTEGER,
    seconds_played  INTEGER,
    time_played     TEXT,
    {", ".join(_col(k) for k in _PLAYER_STAT_KEYS)},
    PRIMARY KEY (event_slug, game_id, person_id)
);

CREATE TABLE IF NOT EXISTS team_periods (
    event_slug  TEXT NOT NULL,
    game_id     INTEGER NOT NULL,
    org_id      INTEGER NOT NULL,
    period      TEXT NOT NULL,
    score       INTEGER,
    PRIMARY KEY (event_slug, game_id, org_id, period)
);

CREATE TABLE IF NOT EXISTS pbp_events (
    event_slug      TEXT NOT NULL,
    game_id         INTEGER NOT NULL,
    "order"         INTEGER NOT NULL,
    period          TEXT,
    event_id        INTEGER,
    act             TEXT,
    action_code     TEXT,
    clock           TEXT,
    game_seconds    INTEGER,   -- elapsed seconds from tip
    score_a         INTEGER,
    score_b         INTEGER,
    org_id          INTEGER,
    person_id       INTEGER,
    person_id_2     INTEGER,
    text            TEXT,
    sub_direction   TEXT,
    made            INTEGER,
    shot_points     INTEGER,
    x               INTEGER,
    y               INTEGER,
    PRIMARY KEY (event_slug, game_id, "order")
);

CREATE TABLE IF NOT EXISTS stints (
    event_slug   TEXT NOT NULL,
    game_id      INTEGER NOT NULL,
    org_id       INTEGER NOT NULL,
    stint_index  INTEGER NOT NULL,
    lineup_key   TEXT,      -- sorted person_ids, comma separated
    start_seconds INTEGER,
    end_seconds   INTEGER,
    seconds       INTEGER,
    points_for    INTEGER,
    points_against INTEGER,
    PRIMARY KEY (event_slug, game_id, org_id, stint_index)
);

CREATE INDEX IF NOT EXISTS idx_pbp_game ON pbp_events (event_slug, game_id);
CREATE INDEX IF NOT EXISTS idx_player_game ON player_game_stats (event_slug, org_id);
CREATE INDEX IF NOT EXISTS idx_team_game ON team_game_stats (event_slug, org_id);
CREATE INDEX IF NOT EXISTS idx_stints ON stints (event_slug, org_id);
"""


def connect(path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _upsert(conn, table: str, rows: Iterable[dict], keys: list[str]) -> None:
    rows = list(rows)
    if not rows:
        return
    columns = list(rows[0])
    quoted = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f'"{c}"=excluded."{c}"' for c in columns if c not in keys)
    conflict = ", ".join(f'"{k}"' for k in keys)
    sql = (
        f"INSERT INTO {table} ({quoted}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
        if updates
        else f"INSERT OR REPLACE INTO {table} ({quoted}) VALUES ({placeholders})"
    )
    conn.executemany(sql, [tuple(r[c] for c in columns) for r in rows])


def upsert_schedule(conn, event_slug: str, games: list[dict]) -> None:
    """Write schedule rows, never blanking a value we already hold.

    FIBA occasionally serves a schedule page where some games parse to all
    nulls: no teams, no tip time, no status. Observed once in a two-day soak,
    on the ten games of the opening day. A plain upsert wrote those nulls
    straight over good data, and a game with no `game_utc` drops out of both the
    fixture list and the window the poller uses to decide when to poll fast and
    when to let the Mac sleep, so a badly timed blip could have slept through a
    match day.

    COALESCE keeps the stored value whenever the incoming one is null, so a
    degraded page is a no-op instead of a regression. Every column here only ever
    goes from null to a value in real life: knockout ties gain teams as the
    bracket resolves, `statusCode` moves INIT to VALID, scores fill in. Nothing
    legitimately reverts to null.
    """
    rows = [dict(g, event_slug=event_slug) for g in games]
    if not rows:
        return

    blank = [r["game_id"] for r in rows
             if not r.get("team_a_code") and not r.get("game_datetime")
             and not r.get("status_code")]
    if blank:
        log.warning("schedule page returned %d game(s) with no data (%s...); "
                    "keeping what we already hold", len(blank), blank[:3])

    columns = list(rows[0])
    keys = ["event_slug", "game_id"]
    quoted = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f'"{c}"=COALESCE(excluded."{c}", games."{c}")'
                        for c in columns if c not in keys)
    conn.executemany(
        f"INSERT INTO games ({quoted}) VALUES ({placeholders}) "
        f'ON CONFLICT("event_slug", "game_id") DO UPDATE SET {updates}',
        [tuple(r[c] for c in columns) for r in rows],
    )
    conn.commit()


def stored_status(conn, event_slug: str) -> dict[int, str]:
    cur = conn.execute(
        "SELECT game_id, status_code FROM games WHERE event_slug=?", (event_slug,)
    )
    return {row["game_id"]: row["status_code"] for row in cur}


def scraped_game_ids(conn, event_slug: str) -> set[int]:
    cur = conn.execute(
        "SELECT game_id FROM games WHERE event_slug=? AND parsed_at IS NOT NULL",
        (event_slug,),
    )
    return {row["game_id"] for row in cur}
