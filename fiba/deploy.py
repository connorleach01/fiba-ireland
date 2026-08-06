"""Publish the built site to GitHub Pages.

The site is the `docs/` directory on the main branch, which GitHub Pages serves
directly. Publishing is therefore just a commit and a push, with no build step
and nothing to configure per deploy.

Deploy failures never abort a poll: a coach losing the newest page for five
minutes is a nuisance, but losing the scrape that produced it is not recoverable
without refetching, so the data path always wins.

A successful `git push` is NOT a successful deploy. On the opening day of the
2026 U16 event GitHub's Pages deployment service degraded: three consecutive
deployments sat in `deployment_queued` or `deployment_in_progress` until the
workflow's ten minute limit aborted them, with no incident published and no
change on our side (the same 122 file, 22 MB tree had deployed in 43 seconds the
evening before). Because publishing only ever checked that the push succeeded,
the poller logged `published` each time and the live site stayed three hours
stale, missing two results and the Portugal scouting page entirely.

So publishing now stamps a build id into `docs/version.txt` and `ensure_live()`
reads that file back off the public URL to confirm the deploy actually landed.
Any mismatch re-triggers a deploy. The check runs once per poll cycle rather than
inline after the push, so a slow deploy never delays the next scrape, and it
recovers from failures we have not seen as well as the one we have.
"""
from __future__ import annotations

import datetime
import logging
import re
import subprocess

import requests

from .config import DATA_DIR, REPORTS_DIR, ROOT

log = logging.getLogger(__name__)

VERSION_FILE = REPORTS_DIR / "version.txt"

# GitHub Pages serves with `cache-control: max-age=600`, so a plain GET can
# report success from cache long after a deploy failed. Every read is
# cache-busted with the id we are looking for.
_CONFIRM_TIMEOUT_S = 15.0

# Re-triggering costs an empty commit and a workflow run, and crucially GitHub
# CANCELS an in-flight Pages deploy when a newer one supersedes it. Under the
# degraded service seen on day one a deploy needed 4 to 7 minutes, so pushing
# again too soon does not retry the deploy, it kills it: one run was cancelled
# at 7m0s, seconds from finishing. The cooldown has to exceed a slow deploy.
RETRIGGER_COOLDOWN_S = 12 * 60

# Held on disk, not just in memory. `launchctl kickstart` restarts the poller
# with a fresh process and would otherwise reset the cooldown to "never", so a
# couple of restarts in quick succession could cancel a deploy twice over.
_RETRIGGER_STAMP = DATA_DIR / "last_redeploy"


def _last_retrigger() -> float:
    try:
        return float(_RETRIGGER_STAMP.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _mark_retrigger(now: float) -> None:
    try:
        _RETRIGGER_STAMP.write_text(f"{now}\n")
    except OSError as exc:
        log.warning("could not record redeploy time: %s", exc)


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


def site_url() -> str | None:
    """Public Pages URL, derived from the origin remote rather than configured.

    Keeping it derived means a fork or a renamed repo cannot end up confirming
    its deploys against somebody else's live site.
    """
    try:
        remote = _git("remote", "get-url", "origin").stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    m = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", remote)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return f"https://{owner.lower()}.github.io/{repo}"


def stamp_build() -> str:
    """Write a fresh build id into docs/version.txt and return it."""
    build_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    VERSION_FILE.write_text(build_id + "\n")
    return build_id


def local_build_id() -> str | None:
    try:
        return VERSION_FILE.read_text().strip() or None
    except OSError:
        return None


def live_build_id() -> str | None:
    """Read version.txt back off the public site, bypassing the CDN cache.

    Returns the live build id, or `""` when the site answered but has no version
    file at all, or None when we could not reach it. The empty string matters:
    a clean 404 is positive evidence that the deploy carrying version.txt never
    landed, which is precisely the failure this whole mechanism exists to catch.
    Collapsing it into None would make the check blind on the first bad deploy.
    """
    base = site_url()
    if not base:
        return None
    try:
        r = requests.get(f"{base}/version.txt", params={"cb": local_build_id() or "0"},
                         timeout=_CONFIRM_TIMEOUT_S)
    except requests.RequestException as exc:
        log.debug("could not read live build id: %s", exc)
        return None
    if r.status_code == 404:
        return ""
    if r.status_code != 200:
        return None
    return r.text.strip() or ""


def ensure_live(now: float) -> bool:
    """Confirm the live site matches the last build, re-triggering if it does not.

    Returns True when the live site is confirmed current. A None from
    `live_build_id` means we could not tell (offline, DNS, a 404 before the very
    first deploy), and "cannot tell" is deliberately not treated as "stale":
    re-triggering on every failed lookup would push empty commits forever while
    the Mac is off the network.
    """
    local = local_build_id()
    if not local or not is_repo() or not has_remote():
        return False

    live = live_build_id()
    if live is None:
        log.debug("live build id unavailable, not treating as stale")
        return False
    if live == local:
        return True

    log.warning("live site is stale: serving %s, built %s",
                live or "no version file", local)
    since = now - _last_retrigger()
    if since < RETRIGGER_COOLDOWN_S:
        log.info("deploy re-trigger on cooldown, %ds remaining "
                 "(a deploy may still be running; pushing now would cancel it)",
                 int(RETRIGGER_COOLDOWN_S - since))
        return False

    try:
        _git("commit", "--allow-empty", "-m", f"Redeploy {local}: live site stale")
        _git("push")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.error("could not re-trigger deploy: %s",
                  getattr(exc, "stderr", "") or exc)
        return False

    _mark_retrigger(now)
    log.info("re-triggered deploy for build %s", local)
    return False


def publish(message: str = "Update reports") -> bool:
    """Commit any change under docs/ and push. Returns True if anything shipped."""
    if not is_repo():
        log.debug("not a git repository, skipping publish")
        return False

    try:
        # Check for real changes BEFORE stamping. Stamping first would rewrite
        # version.txt on every rebuild, so `git status` would never be clean and
        # every restart would push a commit whose only content was a new build
        # id. Each of those pushes cancels the in-flight Pages deploy, which is
        # how a run that had been going seven minutes died three seconds short.
        _git("add", "--", str(REPORTS_DIR.relative_to(ROOT)))
        status = _git("status", "--porcelain", "--", str(REPORTS_DIR.relative_to(ROOT)))
        if not status.stdout.strip():
            log.debug("no report changes to publish")
            return False

        # Something really did change, so stamp it and stage the id alongside
        # the reports it identifies. Confirming a deploy means confirming that
        # exact pair arrived together.
        stamp_build()
        _git("add", "--", str(REPORTS_DIR.relative_to(ROOT)))

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
