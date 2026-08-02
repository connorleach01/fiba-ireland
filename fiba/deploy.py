"""Publish the built site to GitHub Pages.

The site is the `docs/` directory on the main branch, which GitHub Pages serves
directly. Publishing is therefore just a commit and a push, with no build step
and nothing to configure per deploy.

Deploy failures never abort a poll: a coach losing the newest page for five
minutes is a nuisance, but losing the scrape that produced it is not recoverable
without refetching, so the data path always wins.
"""
from __future__ import annotations

import logging
import subprocess

from .config import REPORTS_DIR, ROOT

log = logging.getLogger(__name__)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        check=check, timeout=120,
    )


def is_repo() -> bool:
    try:
        _git("rev-parse", "--is-inside-work-tree")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def has_remote() -> bool:
    try:
        return bool(_git("remote").stdout.strip())
    except subprocess.CalledProcessError:
        return False


def publish(message: str = "Update reports") -> bool:
    """Commit any change under docs/ and push. Returns True if anything shipped."""
    if not is_repo():
        log.debug("not a git repository, skipping publish")
        return False

    try:
        _git("add", "--", str(REPORTS_DIR.relative_to(ROOT)))
        status = _git("status", "--porcelain", "--", str(REPORTS_DIR.relative_to(ROOT)))
        if not status.stdout.strip():
            log.debug("no report changes to publish")
            return False

        _git("commit", "-m", message)
        if has_remote():
            _git("push")
            log.info("published: %s", message)
        else:
            log.info("committed locally (no remote configured): %s", message)
        return True
    except subprocess.CalledProcessError as exc:
        # Surface it loudly but let the caller carry on.
        log.error("publish failed: %s", (exc.stderr or exc.stdout or "").strip()[:400])
        return False
    except subprocess.TimeoutExpired:
        log.error("publish timed out")
        return False
