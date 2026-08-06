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

# Polling is adaptive, because a flat interval is either slow when it matters or
# wasteful when it does not. The schedule gives every tip time, so we know when a
# result can physically land: a 40-minute FIBA game with breaks runs about 100
# minutes, and overtime or a long stoppage stretches that. Inside a window that
# could produce a final we poll every FAST_INTERVAL_S; outside one, every
# IDLE_INTERVAL_S.
#
# Measured over match day one this is about 770 requests against 288 for a flat
# 5 minutes, so it is more traffic, not less, but all of it lands in the five
# 105-minute windows where a result can actually appear, and the overnight and
# rest-day hours drop to a quarter of the old rate. One lightweight schedule page
# every 45s while a game is finishing is a fair trade for cutting the average
# detection delay from 2.5 minutes to about 22 seconds.
FAST_INTERVAL_S = 45
IDLE_INTERVAL_S = 900
FINISH_WINDOW_START_S = 75 * 60
FINISH_WINDOW_END_S = 3 * 3600

# FIBA flips `statusCode` to VALID several minutes before it publishes the box
# score, so the game page parses to nothing in between. Measured across the four
# games of match day one, that gap ran 250 to 495 seconds (mean 371), and the
# poller burned 6 to 11 failed attempts waiting it out.
#
# Nothing here can shorten FIBA's gap. What it can shorten is the tail: at a 45s
# cadence the data sat published for an average of 25 seconds before we looked
# again. Once a game is known VALID-but-unparseable we are no longer hunting for
# a result, we are waiting on a specific page to fill in, so poll it tightly and
# cut that tail to about 6 seconds. It costs a few dozen extra requests confined
# to the minutes a game is actually landing.
PENDING_INTERVAL_S = 12

# Consecutive result windows on a match day are only about 45 minutes apart, and
# the machine is woken by a single daily `pmset repeat`. Releasing the sleep hold
# in one of those gaps would let the Mac sleep at, say, 6am with nothing to wake
# it for the 6:45am window, so the hold bridges any gap shorter than this. The
# overnight gap is twelve hours and is far past it, which is the one we do want
# to sleep through.
SLEEP_HOLD_BRIDGE_S = 100 * 60


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


def next_interval(conn, event_slug: str, override: int | None = None,
                  pending: int = 0) -> tuple[int, str]:
    """How long to wait before the next poll, and why.

    Returns the fast interval whenever a game that has already tipped could still
    be publishing its result, so the gap between the final buzzer and the report
    is seconds rather than minutes. Falls back to the idle interval the rest of
    the time. `override` is the `--interval` flag, which pins both.

    `pending` is the count of games that are final but did not parse on the last
    cycle. That is the tightest signal available: the result exists, FIBA has
    simply not finished publishing it, and it will appear within minutes.
    """
    if override is not None:
        return override, "fixed"

    if pending:
        return PENDING_INTERVAL_S, f"{pending} game(s) final but not yet published"

    now = dt.datetime.now(dt.timezone.utc)
    earliest = (now - dt.timedelta(seconds=FINISH_WINDOW_END_S)).isoformat()
    latest = (now - dt.timedelta(seconds=FINISH_WINDOW_START_S)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) n FROM games WHERE event_slug=? AND game_utc IS NOT NULL "
        "AND game_utc BETWEEN ? AND ? AND parsed_at IS NULL",
        (event_slug, earliest, latest)).fetchone()
    if row and row["n"]:
        return FAST_INTERVAL_S, f"{row['n']} game(s) due to finish"

    # Nothing due right now. Sleep until shortly before the next window opens
    # rather than idling blindly past it, capped so a stalled clock cannot park
    # the poller for hours.
    upcoming = conn.execute(
        "SELECT MIN(game_utc) t FROM games WHERE event_slug=? AND game_utc > ? "
        "AND parsed_at IS NULL", (event_slug, latest)).fetchone()
    if upcoming and upcoming["t"]:
        try:
            tip = dt.datetime.fromisoformat(upcoming["t"])
            wait = (tip - now).total_seconds() + FINISH_WINDOW_START_S
            if 0 < wait < IDLE_INTERVAL_S:
                return max(FAST_INTERVAL_S, int(wait)), "next result window opening"
        except ValueError:
            pass
    return IDLE_INTERVAL_S, "no game due"


def sleep_hold_needed(conn, event_slug: str) -> bool:
    """Should the Mac be kept awake right now?

    True inside a result window, and also in the short gaps between the windows
    of a match day, because only one wake is scheduled per day and a machine that
    sleeps in a 45-minute gap has nothing to wake it for the next game. False for
    the long overnight and rest-day gaps, which is the whole point.
    """
    now = dt.datetime.now(dt.timezone.utc)
    earliest = (now - dt.timedelta(seconds=FINISH_WINDOW_END_S)).isoformat()
    latest = (now - dt.timedelta(seconds=FINISH_WINDOW_START_S)).isoformat()
    if conn.execute(
        "SELECT 1 FROM games WHERE event_slug=? AND game_utc IS NOT NULL "
        "AND game_utc BETWEEN ? AND ? AND parsed_at IS NULL LIMIT 1",
        (event_slug, earliest, latest)).fetchone():
        return True

    row = conn.execute(
        "SELECT MIN(game_utc) t FROM games WHERE event_slug=? AND game_utc > ? "
        "AND parsed_at IS NULL", (event_slug, latest)).fetchone()
    if not row or not row["t"]:
        return False
    try:
        opens = dt.datetime.fromisoformat(row["t"]) + dt.timedelta(
            seconds=FINISH_WINDOW_START_S)
    except ValueError:
        return False
    return 0 <= (opens - now).total_seconds() <= SLEEP_HOLD_BRIDGE_S


class SleepBlocker:
    """Holds off system sleep only while a result is actually due.

    Wrapping the whole poller in `caffeinate` kept the Mac awake around the
    clock, including the 12 hours overnight and the 22 to 36 hours of a rest day,
    which is a lot of machine time to buy nothing. This asserts inside a result
    window and releases outside one, so the Mac is free to sleep whenever no game
    can produce a score.

    `caffeinate -s` is inert on battery by design, so this is a no-op unless the
    laptop is plugged in.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def hold(self, wanted: bool) -> None:
        if wanted and self._proc is None:
            try:
                self._proc = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-s"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log.info("holding off system sleep while results are due")
            except Exception as exc:  # noqa: BLE001 - never let this end the watch
                log.warning("could not hold off sleep: %s", exc)
        elif not wanted and self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self._proc.kill()
            self._proc = None
            log.info("released sleep hold; the Mac may sleep until the next window")

    def release(self) -> None:
        self.hold(False)


def sleep_until(seconds: float, step: float = 10.0) -> None:
    """Sleep `seconds` measured on the wall clock, not the monotonic one.

    macOS does not advance the monotonic clock while the system is asleep, so a
    plain `time.sleep(900)` started before a suspend still has most of its 900s
    left on wake, delaying the first poll of the morning by up to fifteen
    minutes. Waking in short steps and re-checking the wall clock means a
    suspend-and-resume falls straight through and polls immediately.

    `step` is deliberately short. A scheduled wake with the lid closed is a
    DarkWake: the machine comes up with the display off and returns to sleep
    quickly unless something takes a power assertion. The poller only takes one
    after its next poll, so a long step risks losing that race and sleeping again
    before any work happens. Ten seconds costs nothing and leaves margin.
    """
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    while True:
        remaining = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


def cycle(conn, event_slug: str, org_id: int = IRELAND_ORG_ID,
          rebuild_always: bool = False, publish: bool = True) -> dict:
    """One poll: refresh schedule, ingest new finals, rebuild if anything landed."""
    summary = ingest.sync_event(conn, event_slug)
    new_games = summary["ingested"]

    # One line every cycle, whether or not anything happened. A healthy poll used
    # to log nothing at all, which meant an operator glancing at the log could not
    # tell "polling fine, no new results" from "process wedged two hours ago".
    # During the tournament this is the line you watch: `final` counts up as games
    # end, and a timestamp that has stopped moving is the alarm.
    log.info("poll ok: %d scheduled, %d final, %d new",
             summary["scheduled"], summary["final"], len(new_games))

    if new_games or rebuild_always:
        report.build_all(conn, event_slug, org_id)
        log.info("rebuilt reports into %s", REPORTS_DIR)
        if publish:
            label = (f"Add {len(new_games)} result(s)" if new_games
                     else "Refresh reports")
            deploy.publish(label)

    # Checked every cycle, not just after a publish. A push that GitHub never
    # deployed leaves the site stale indefinitely, because nothing else would
    # look again until the next game happened to finish. One cheap GET here
    # closes that gap and costs nothing when everything is healthy.
    if publish:
        deploy.ensure_live(time.time())

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

    _report_failures(summary["failed"], time.time())
    return summary


# A game that is final but has not parsed yet is the normal state of affairs for
# the first few minutes after the buzzer, not a fault. Measured across match day
# one the wait ran up to 495 seconds, so anything inside the grace period is
# logged quietly and never notified. Past it, something is actually wrong and
# both the log and a notification should say so, once.
PUBLISH_GRACE_S = 12 * 60

_first_failure: dict[int, float] = {}
_alerted: set[int] = set()


def _report_failures(failed: list[tuple[int, str]], now: float) -> None:
    """Log parse failures, escalating only once a game is late rather than slow.

    Without this the poller shouted on every cycle: match day one produced 61
    ERROR lines and a notification per cycle per game for what was ordinary
    publishing lag. At the pending interval that would be several hundred. The
    signal that matters is a game still unparsed well past the observed lag, and
    that fires exactly once.
    """
    still_failing = {game_id for game_id, _ in failed}
    for game_id in list(_first_failure):
        if game_id not in still_failing:
            del _first_failure[game_id]
            _alerted.discard(game_id)

    for game_id, message in failed:
        waited = now - _first_failure.setdefault(game_id, now)
        if waited < PUBLISH_GRACE_S:
            log.info("%s final, box score not published yet (%ds)", game_id, int(waited))
        elif game_id not in _alerted:
            _alerted.add(game_id)
            log.error("could not ingest %s after %ds: %s", game_id, int(waited), message)
            notify("FIBA scrape problem",
                   f"game {game_id} has not parsed in {int(waited // 60)} minutes")
        else:
            log.warning("%s still unparsed after %ds: %s", game_id, int(waited), message)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event", default=EVENT_SLUG, help="event slug to watch")
    parser.add_argument("--org", type=int, default=IRELAND_ORG_ID,
                        help="subject team organisation id")
    parser.add_argument("--once", action="store_true", help="run a single cycle")
    parser.add_argument("--interval", type=int, default=None,
                        help="pin the poll gap in seconds, disabling adaptive polling")
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

    pinned = args.interval if args.interval is not None else None
    log.info("watching %s; %s; reports in %s", args.event,
             f"every {pinned}s" if pinned else
             f"{FAST_INTERVAL_S}s while a result is due, else {IDLE_INTERVAL_S}s",
             REPORTS_DIR)
    first = True
    blocker = SleepBlocker()
    try:
        while True:
            pending = 0
            try:
                summary = cycle(conn, args.event, args.org, rebuild_always=first,
                                publish=not args.no_publish)
                # Both kinds mean "a result is imminent", so both justify the
                # tight poll; only `failed` is worth reporting.
                pending = len(summary["failed"]) + len(summary.get("probing", []))
                first = False
            except KeyboardInterrupt:
                log.info("stopped")
                return 0
            except Exception as exc:  # noqa: BLE001 - a poll failure must not end the watch
                log.exception("poll failed, continuing: %s", exc)
            try:
                wait, why = next_interval(conn, args.event, pinned, pending)
            except Exception:  # noqa: BLE001 - scheduling must never end the watch
                wait, why = FAST_INTERVAL_S, "interval lookup failed"
            # Keep the Mac awake through the day's games, but let it sleep
            # through the long gaps. Decided separately from the poll interval:
            # the poller can idle at 15 minutes and still need the machine up.
            try:
                blocker.hold(pinned is None and sleep_hold_needed(conn, args.event))
            except Exception as exc:  # noqa: BLE001 - never end the watch over this
                log.warning("sleep hold check failed: %s", exc)
            log.info("next poll in %ds (%s)", wait, why)
            try:
                sleep_until(wait)
            except KeyboardInterrupt:
                log.info("stopped")
                return 0
    finally:
        blocker.release()


if __name__ == "__main__":
    sys.exit(main())
