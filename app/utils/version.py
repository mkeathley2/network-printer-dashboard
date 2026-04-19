"""
Version utilities: read the installed version and fetch the latest GitHub release.
The GitHub API result is cached for 1 hour to avoid rate-limiting.
"""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

VERSION_FILE = Path("/project/VERSION")
GITHUB_REPO = "mkeathley2/network-printer-dashboard"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Simple in-process cache: (fetched_at, release_dict or None)
_cache: tuple[datetime, dict | None] | None = None
_CACHE_TTL = timedelta(hours=1)


def get_current_version() -> str:
    """Read the VERSION file mounted at /project/VERSION. Returns 'unknown' if not found."""
    try:
        return VERSION_FILE.read_text().strip()
    except Exception:
        return "unknown"


def get_latest_release(force_refresh: bool = False) -> dict | None:
    """
    Fetch the latest GitHub Release for this repo.

    Returns a dict with at minimum:
        tag_name   — e.g. "v0.1.0"
        name       — release title
        body       — markdown release notes
        html_url   — link to the release on GitHub
        published_at — ISO timestamp string

    Returns None on network error, no releases published, or GitHub 404.
    Result is cached for 1 hour.
    """
    global _cache
    now = datetime.utcnow()

    if not force_refresh and _cache is not None:
        fetched_at, data = _cache
        if (now - fetched_at) < _CACHE_TTL:
            return data

    try:
        req = urllib.request.Request(
            GITHUB_API,
            headers={"User-Agent": f"printer-dashboard ({GITHUB_REPO})"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        _cache = (now, data)
        return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # No releases published yet — cache a None so we don't hammer the API
            logger.debug("No GitHub releases found for %s", GITHUB_REPO)
        else:
            logger.warning("GitHub API HTTP %s when checking for updates", e.code)
        _cache = (now, None)
        return None
    except Exception as e:
        logger.warning("Could not fetch GitHub release info: %s", e)
        # Don't cache on transient network errors so it retries next visit
        return None


def update_available() -> bool:
    """Return True if the latest GitHub release tag differs from the installed version."""
    current = get_current_version()
    latest = get_latest_release()
    if not latest or current == "unknown":
        return False
    return latest.get("tag_name", "") != current
