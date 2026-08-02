"""Measure lineup reconstruction accuracy across a whole event.

Run against a completed event to decide, from evidence rather than assumption,
whether derived lineup minutes can be trusted.

    python -m tests.validate_lineups fiba-u16-eurobasket-2025-division-b
"""
from __future__ import annotations

import statistics
import sys
from collections import Counter

from fiba import db, fetch, ingest, lineups, parse


def main(event_slug: str) -> int:
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM games WHERE event_slug=? AND parsed_at IS NOT NULL "
        "ORDER BY game_id",
        (event_slug,),
    ).fetchall()
    if not rows:
        print(f"no ingested games for {event_slug}")
        return 1

    all_deltas: list[int] = []
    per_game_max: list[int] = []
    failures = []
    errors = []
    reasons: Counter[str] = Counter()

    for row in rows:
        html = fetch.cached_game_html(event_slug, row["game_id"])
        if html is None:
            continue
        try:
            game = parse.parse_game(html)
            result, report = lineups.reconstruct(
                game,
                lambda e: ingest.clock_to_elapsed(e["period"], e["clock"]),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append((row["game_id"], str(exc)))
            reasons[type(exc).__name__] += 1
            continue

        for player in game["players"]:
            official = player.get("seconds_played")
            if official is None:
                continue
            derived = result["player_seconds"].get(player["person_id"], 0)
            all_deltas.append(abs(derived - official))

        per_game_max.append(report["max_error_seconds"])
        if not report["ok"]:
            failures.append((row["game_id"], report))

    print(f"event: {event_slug}")
    print(f"games checked: {len(per_game_max)}   hard errors: {len(errors)}")
    for game_id, message in errors[:5]:
        print(f"  ERROR {game_id}: {message[:140]}")

    if all_deltas:
        print(f"\nplayer-minute deltas (|derived - official|, seconds): "
              f"n={len(all_deltas)}")
        print(f"  exact (0s)  : {sum(1 for d in all_deltas if d == 0)/len(all_deltas):6.1%}")
        print(f"  within 5s   : {sum(1 for d in all_deltas if d <= 5)/len(all_deltas):6.1%}")
        print(f"  within 30s  : {sum(1 for d in all_deltas if d <= 30)/len(all_deltas):6.1%}")
        print(f"  median      : {statistics.median(all_deltas):.0f}s")
        print(f"  p95         : {sorted(all_deltas)[int(0.95*len(all_deltas))]:.0f}s")
        print(f"  worst       : {max(all_deltas)}s")

    if per_game_max:
        clean = sum(1 for m in per_game_max if m <= lineups.TOLERANCE_SECONDS)
        print(f"\ngames within {lineups.TOLERANCE_SECONDS}s tolerance: "
              f"{clean}/{len(per_game_max)} ({clean/len(per_game_max):.1%})")

    for game_id, report in failures[:5]:
        worst = sorted(report["discrepancies"], key=lambda d: -abs(d["delta"]))[:3]
        print(f"\n  game {game_id}: max error {report['max_error_seconds']}s, "
              f"{len(report['size_errors'])} bad lineup sizes")
        for entry in worst:
            print(f"    {entry['name']:<28} official {entry['official']:>5}s "
                  f"derived {entry['derived']:>5}s  delta {entry['delta']:+}s")

    return 0


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "fiba-u16-eurobasket-2025-division-b"
    raise SystemExit(main(slug))
