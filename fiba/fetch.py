"""Retrieval of FIBA event pages.

Every game page we download is kept on disk gzipped so that a parser fix can be
replayed offline without hitting FIBA again. That cache is the insurance policy
if the site changes shape mid-tournament.
"""
from __future__ import annotations

import gzip
import logging
import os
import time

import requests

from .config import BASE_URL, RAW_DIR

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

REQUEST_SPACING_S = 2.0
MAX_ATTEMPTS = 4

_session: requests.Session | None = None
_last_request_at = 0.0


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )
    return _session


def _throttle() -> None:
    global _last_request_at
    wait = REQUEST_SPACING_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def http_get(url: str) -> str:
    """GET with polite spacing and backoff on transient failures."""
    session = _get_session()
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _throttle()
        try:
            response = session.get(url, timeout=30)
            if response.status_code == 200:
                return response.text
            # 4xx other than 429 will not fix itself, so fail immediately.
            if response.status_code < 500 and response.status_code != 429:
                raise RuntimeError(f"HTTP {response.status_code} for {url}")
            last_error = RuntimeError(f"HTTP {response.status_code} for {url}")
        except requests.RequestException as exc:
            last_error = exc

        if attempt < MAX_ATTEMPTS:
            backoff = 2.0**attempt
            log.warning("attempt %d/%d failed for %s, retrying in %.0fs (%s)",
                        attempt, MAX_ATTEMPTS, url, backoff, last_error)
            time.sleep(backoff)

    raise RuntimeError(f"giving up on {url} after {MAX_ATTEMPTS} attempts") from last_error


def schedule_url(event_slug: str) -> str:
    return f"{BASE_URL}/en/events/{event_slug}/games"


def game_url(event_slug: str, game_id: int, team_a_code: str, team_b_code: str) -> str:
    return f"{BASE_URL}/en/events/{event_slug}/games/{game_id}-{team_a_code}-{team_b_code}"


def fetch_schedule_html(event_slug: str) -> str:
    """The schedule page carries every game in the event in one response."""
    return http_get(schedule_url(event_slug))


def _raw_path(event_slug: str, game_id: int):
    directory = RAW_DIR / event_slug
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{game_id}.html.gz"


def cached_game_html(event_slug: str, game_id: int) -> str | None:
    path = _raw_path(event_slug, game_id)
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return handle.read()


def store_game_html(event_slug: str, game_id: int, html: str) -> None:
    """Write the cache entry atomically.

    The poller and a manual command can run at once, so a reader must never be
    able to observe a half-written file. Write to a temporary name in the same
    directory, then rename, which is atomic on the same filesystem.
    """
    path = _raw_path(event_slug, game_id)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as handle:
            handle.write(html)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def discard_cached_game(event_slug: str, game_id: int) -> None:
    """Remove a cache entry that could not be parsed, so the next poll refetches."""
    path = _raw_path(event_slug, game_id)
    if path.exists():
        path.unlink(missing_ok=True)
        log.info("discarded unparseable cache entry for game %s", game_id)


def fetch_game_html(
    event_slug: str,
    game_id: int,
    team_a_code: str,
    team_b_code: str,
    *,
    use_cache: bool = True,
) -> str:
    """Fetch a game page, preferring the on-disk copy when we already have it."""
    if use_cache:
        cached = cached_game_html(event_slug, game_id)
        if cached is not None:
            log.debug("cache hit for game %s", game_id)
            return cached

    html = http_get(game_url(event_slug, game_id, team_a_code, team_b_code))
    store_game_html(event_slug, game_id, html)
    return html
