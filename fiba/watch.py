"""Poll the event schedule, ingest finished games, rebuild reports.

Usage:
    python -m fiba.watch --once            one cycle, then exit
    python -m fiba.watch                   loop until interrupted
    python -m fiba.watch --backfill SLUG   pull a whole past event
    python -m fiba.watch --rebuild         regenerate reports from stored data

A game becomes available when its schedule status flips from INIT to VALID.
Any newly finished game triggers a full report rebuild, not only Ireland's,
because the scouting reports depend on what every other team has just done.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from . import db, deploy, ingest, report
from .config import EVENT_SLUG, IRELAND_ORG_ID, REPORTS_DIR

log = logging.getLogger("fiba.watch")
IRISH_TZ = ZoneInfo("Europe/Dublin")

DEFAULT_INTERVAL_S = 300


def notify(title: str, message: str) -> None:
    """Best-effort macOS notification. Never let this break the run."""
    try:
        script = (
            f'display notification {_as_applescript(message)} '
            f'with title {_as_applescript(title)}'
        )
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        log.debug("notification failed: %s", exc)


def _as_applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cycle(conn, event_slug: str, org_id: int = IRELAND_ORG_ID,
          rebuild_always: bool = False, publish: bool = True) -> dict:
    """One poll: refresh schedule, ingest new finals, rebuild if anything landed."""
    summary = ingest.sync_event(conn, event_slug)
    new_games = summary["ingested"]

    if new_games or rebuild_always:
        report.build_all(conn, event_slug, org_id)
        log.info("rebuilt reports into %s", REPORTS_DIR)
        if publish:
            label = (f"Add {len(new_games)} result(s)" if new_games
                     else "Refresh reports")
            deploy.publish(label)

    if new_games:
        described = []
        for game_id in new_games:
            row = conn.execute(
                "SELECT team_a_code, team_b_code, team_a_score, team_b_score "
                "FROM games WHERE event_slug=? AND game_id=?",
                (event_slug, game_id)).fetchone()
            if row:
                described.append(
                    f"{row['team_a_code']} {row['team_a_score']}"
                    f"-{row['team_b_score']} {row['team_b_code']}"
                )
        headline = ", ".join(described[:3])
        if len(described) > 3:
            headline += f" and {len(described) - 3} more"
        log.info("new results: %s", headline)
        notify("FIBA reports updated", headline or f"{len(new_games)} new games")

    if summary["failed"]:
        for game_id, message in summary["failed"]:
            log.error("could not ingest %s: %s", game_id, message)
        notify("FIBA scrape problem",
               f"{len(summary['failed'])} game(s) failed to parse. Check the log.")

    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event", default=EVENT_SLUG, help="event slug to watch")
    parser.add_argument("--org", type=int, default=IRELAND_ORG_ID,
                        help="subject team organisation id")
    parser.add_argument("--once", action="store_true", help="run a single cycle")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S,
                        help="seconds between polls")
    parser.add_argument("--backfill", metavar="SLUG",
                        help="ingest every finished game of an event, then exit")
    parser.add_argument("--rebuild", action="store_true",
                        help="regenerate reports from stored data, then exit")
    parser.add_argument("--example", metavar="SLUG", nargs="?",
                        const="fiba-u18-eurobasket-2026-division-b",
                        help="build a reference report set from a completed event "
                             "into docs/example/, then exit")
    parser.add_argument("--no-publish", action="store_true",
                        help="build reports but do not commit or push the site")
    parser.add_argument("--publish", action="store_true",
                        help="commit and push the current site, then exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = db.connect()
    db.init(conn)

    if args.backfill:
        summary = ingest.sync_event(conn, args.backfill, only_new=False)
        log.info("backfill %s: %d ingested, %d failed",
                 args.backfill, len(summary["ingested"]), len(summary["failed"]))
        for game_id, message in summary["failed"]:
            log.error("  %s: %s", game_id, message)
        return 0

    if args.example:
        # A worked example from a finished event. It lives in its own directory
        # with its own navigation, and every page is stamped so nobody mistakes
        # last season's data for a live result.
        target = REPORTS_DIR / "example"
        target.mkdir(parents=True, exist_ok=True)
        original, report.REPORTS_DIR = report.REPORTS_DIR, target
        try:
            label = args.example.replace("-", " ").title().replace("Fiba", "FIBA")
            written = report.build_all(conn, args.example, args.org, reference=label)
        finally:
            report.REPORTS_DIR = original
        log.info("example built into %s: %d scouts, %d reviews",
                 target, len(written["scouts"]), len(written["reviews"]))
        if not args.no_publish:
            deploy.publish(f"Rebuild example from {args.example}")
        return 0

    if args.publish:
        deploy.publish("Manual publish")
        return 0

    if args.rebuild:
        report.build_all(conn, args.event, args.org)
        log.info("reports rebuilt into %s", REPORTS_DIR)
        if not args.no_publish:
            deploy.publish("Rebuild reports")
        return 0

    if args.once:
        cycle(conn, args.event, args.org, rebuild_always=True,
              publish=not args.no_publish)
        return 0

    log.info("watching %s every %ds; reports in %s",
             args.event, args.interval, REPORTS_DIR)
    first = True
    while True:
        try:
            cycle(conn, args.event, args.org, rebuild_always=first,
                  publish=not args.no_publish)
            first = False
        except KeyboardInterrupt:
            log.info("stopped")
            return 0
        except Exception as exc:  # noqa: BLE001 - a poll failure must not end the watch
            log.exception("poll failed, continuing: %s", exc)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("stopped")
            return 0


if __name__ == "__main__":
    sys.exit(main())
