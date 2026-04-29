from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request
from flask_login import login_required

from app.core.database import db
from app.models import Printer, SupplySnapshot, TelemetrySnapshot
from app.models.alert import AlertEvent
from app.utils.depletion import compute_pages_per_day, compute_supply_depletion
from app.utils.timezone import to_local
from app.web.routes.config import get_effective_thresholds

bp = Blueprint("history", __name__, url_prefix="/history")

# Default time-window for the history page (days).  Filter pills let the user
# switch between 7 / 30 / 90 / All time.
_DEFAULT_DAYS = 90
_VALID_DAYS = {"7", "30", "90", "all"}


def _parse_days(raw: str | None) -> tuple[str, int | None]:
    """
    Returns (canonical_value, window_days_or_None).
    canonical_value is one of '7', '30', '90', 'all'  — used by the template
    to highlight the active pill.
    window_days is the integer for filtering, or None for all-time.
    """
    if raw not in _VALID_DAYS:
        raw = str(_DEFAULT_DAYS)
    if raw == "all":
        return "all", None
    return raw, int(raw)


@bp.route("/<int:printer_id>")
@login_required
def printer_history(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    days_raw, window_days = _parse_days(request.args.get("days"))

    cutoff = (datetime.utcnow() - timedelta(days=window_days)) if window_days else None

    # --- Latest snapshot per supply slot (active supplies) ---
    # Find each distinct supply_index ever seen for this printer; for each,
    # grab the most recent SupplySnapshot row.
    supply_indexes = [
        row[0] for row in db.session.query(SupplySnapshot.supply_index)
        .filter(SupplySnapshot.printer_id == printer_id)
        .distinct()
        .all()
    ]
    latest_supplies = []
    for idx in supply_indexes:
        latest = (
            db.session.query(SupplySnapshot)
            .filter_by(printer_id=printer_id, supply_index=idx)
            .order_by(SupplySnapshot.polled_at.desc())
            .first()
        )
        if latest:
            latest_supplies.append(latest)
    latest_supplies.sort(key=lambda s: s.supply_index)

    # --- Build depletion estimate for each supply ---
    warn_pct, crit_pct = get_effective_thresholds(printer)
    depletions = []
    for s in latest_supplies:
        d = compute_supply_depletion(printer_id, s.supply_index, db.session,
                                     window_days=window_days)
        # Determine status from current level
        cur = float(s.level_pct) if s.level_pct is not None else None
        if cur is None:
            status = "unknown"
        elif cur < crit_pct:
            status = "critical"
        elif cur < warn_pct:
            status = "warning"
        else:
            status = "ok"

        depletions.append({
            "supply_index": s.supply_index,
            "supply_color": s.supply_color or "unknown",
            "supply_type": s.supply_type or "tonerCartridge",
            "description": s.supply_description or f"{(s.supply_color or 'Supply').title()} {s.supply_index}",
            "current_pct": cur,
            "status": status,
            "depletion": d,  # dict or None
            "last_polled_at": s.polled_at,
        })

    # --- Pages per day average over the window ---
    pages_per_day = compute_pages_per_day(printer_id, db.session, window_days=window_days)

    # --- Replacement events within the window (for chart annotations) ---
    rep_q = db.session.query(AlertEvent).filter(
        AlertEvent.printer_id == printer_id,
        AlertEvent.event_type.in_(("toner_replaced", "drum_replaced")),
    )
    if cutoff is not None:
        rep_q = rep_q.filter(AlertEvent.occurred_at >= cutoff)
    replacement_events = rep_q.order_by(AlertEvent.occurred_at.asc()).all()

    # Build the Chart.js annotation config server-side so the template just inlines it.
    # Each annotation is a vertical line at the event timestamp with a small label.
    color_for_event = {
        "black": "rgba(33,37,41,0.55)",
        "cyan": "rgba(13,202,240,0.65)",
        "magenta": "rgba(214,51,132,0.65)",
        "yellow": "rgba(255,193,7,0.75)",
    }
    annotations = {}
    for i, evt in enumerate(replacement_events):
        color = color_for_event.get(
            (evt.supply_color or "").lower(),
            "rgba(220,53,69,0.55)",  # default red-ish
        )
        label = "Replaced"
        if evt.supply_color:
            label = f"Replaced ({evt.supply_color.title()})"
        annotations[f"rep_{i}"] = {
            "type": "line",
            "xMin": to_local(evt.occurred_at).isoformat(),
            "xMax": to_local(evt.occurred_at).isoformat(),
            "borderColor": color,
            "borderWidth": 1.5,
            "borderDash": [4, 4],
            "label": {
                "display": True,
                "content": label,
                "position": "start",
                "backgroundColor": "rgba(255,255,255,0.85)",
                "color": "#212529",
                "font": {"size": 10},
                "padding": 3,
                "borderRadius": 3,
            },
        }

    return render_template(
        "history/printer.html",
        printer=printer,
        days=days_raw,
        window_days=window_days,
        depletions=depletions,
        pages_per_day=pages_per_day,
        replacement_events=replacement_events,
        annotations_json=json.dumps(annotations),
        warn_pct=warn_pct,
        crit_pct=crit_pct,
    )
