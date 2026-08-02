"""Game clock conversions, shared by ingest and lineup reconstruction."""
from __future__ import annotations

from .config import OT_SECONDS, PERIOD_SECONDS


def period_index(label: str | None) -> int:
    """Q1..Q4 -> 0..3, OT1 -> 4, and so on."""
    text = (label or "").upper()
    if text.startswith("Q"):
        try:
            return int(text[1:]) - 1
        except ValueError:
            return 0
    if text.startswith("OT"):
        try:
            return 3 + int(text[2:] or 1)
        except ValueError:
            return 4
    return 0


def period_bounds(index: int) -> tuple[int, int]:
    """Seconds elapsed before a period starts, and that period's length."""
    if index < 4:
        return index * PERIOD_SECONDS, PERIOD_SECONDS
    return 4 * PERIOD_SECONDS + (index - 4) * OT_SECONDS, OT_SECONDS


def clock_to_elapsed(period_label: str | None, clock: str | None) -> int | None:
    """Convert a period label plus remaining clock into seconds since tip."""
    if not clock or ":" not in clock:
        return None
    minutes, _, seconds = clock.partition(":")
    try:
        remaining = int(minutes) * 60 + int(seconds)
    except ValueError:
        return None
    before, length = period_bounds(period_index(period_label))
    return before + (length - remaining)


def elapsed_of(event: dict) -> int | None:
    """Convenience wrapper for a play-by-play event dict."""
    return clock_to_elapsed(event.get("period"), event.get("clock"))
