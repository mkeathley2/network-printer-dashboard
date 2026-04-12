"""Timezone conversion helpers.

Uses zoneinfo (stdlib, Python 3.9+). The tzdata package provides the
timezone database on systems that don't ship one (e.g. Docker/Windows).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_site_tz() -> ZoneInfo:
    """Return the configured site timezone, falling back to UTC."""
    try:
        from app.core.database import db
        from app.models import SiteSetting
        row = db.session.get(SiteSetting, "timezone")
        tz_name = row.value if (row and row.value) else "America/Chicago"
    except Exception:
        tz_name = "America/Chicago"
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        return ZoneInfo("UTC")


def to_local(dt: datetime | None) -> datetime | None:
    """Convert a naive UTC datetime to the site's local timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(get_site_tz())


# Common timezone choices for the dropdown
TIMEZONE_CHOICES = [
    ("America/New_York",    "Eastern (ET)"),
    ("America/Chicago",     "Central (CT)"),
    ("America/Denver",      "Mountain (MT)"),
    ("America/Phoenix",     "Arizona (no DST)"),
    ("America/Los_Angeles", "Pacific (PT)"),
    ("America/Anchorage",   "Alaska (AKT)"),
    ("Pacific/Honolulu",    "Hawaii (HT)"),
    ("UTC",                 "UTC"),
]
