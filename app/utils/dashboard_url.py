"""
Helpers for building absolute dashboard URLs that work inside emails
(alerts, helpdesk tickets, scheduled reports).

The base URL comes from the ``public_url`` SiteSetting that the admin
configures under Config → Remote Agents.  When it isn't set, every
helper returns ``None`` so callers can skip rendering links instead of
showing broken / relative URLs.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_dashboard_url() -> Optional[str]:
    """Return the configured public URL (no trailing slash), or None."""
    try:
        from app.core.database import db
        from app.models import SiteSetting

        row = db.session.get(SiteSetting, "public_url")
        if row and row.value:
            return row.value.rstrip("/")
    except Exception:
        logger.debug("Could not read public_url setting", exc_info=True)
    return None


def printer_url(printer_id: int, anchor: Optional[str] = None) -> Optional[str]:
    """
    Absolute URL to a printer's detail page, or None when public_url is unset.

    Optional ``anchor`` jumps to a section on the page (e.g. ``"replacements"``
    to land on the Toner Replacement History card).
    """
    base = get_dashboard_url()
    if not base:
        return None
    url = f"{base}/printers/{printer_id}"
    if anchor:
        url += f"#{anchor.lstrip('#')}"
    return url


def history_url(printer_id: int) -> Optional[str]:
    """Absolute URL to a printer's history page, or None when public_url is unset."""
    base = get_dashboard_url()
    return f"{base}/history/{printer_id}" if base else None


def report_url(report_path: str) -> Optional[str]:
    """
    Absolute URL to a reports page.

    Pass the path with leading slash, e.g. ``"/reports/toner-cost"``.
    """
    base = get_dashboard_url()
    if not base:
        return None
    if not report_path.startswith("/"):
        report_path = "/" + report_path
    return f"{base}{report_path}"
