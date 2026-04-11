from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app.core.database import db
from app.models import AlertEvent, Printer, SupplySnapshot, TelemetrySnapshot
from app.models.location import Location
from app.web.routes.config import get_effective_thresholds

bp = Blueprint("dashboard", __name__)


def _build_printer_data(printers: list) -> list:
    """Fetch latest telemetry + supplies for each printer in one pass."""
    printer_data = []
    for p in printers:
        latest = (
            db.session.query(TelemetrySnapshot)
            .filter_by(printer_id=p.id)
            .order_by(TelemetrySnapshot.polled_at.desc())
            .first()
        )
        supplies = []
        if latest:
            supplies = (
                db.session.query(SupplySnapshot)
                .filter_by(telemetry_id=latest.id)
                .order_by(SupplySnapshot.supply_index)
                .all()
            )
        warn_pct, crit_pct = get_effective_thresholds(p)
        printer_data.append({"printer": p, "telemetry": latest, "supplies": supplies,
                              "warn_pct": warn_pct, "crit_pct": crit_pct})
    return printer_data


@bp.route("/")
@login_required
def index():
    # Summary stats
    total = db.session.query(Printer).filter_by(is_active=True).count()
    online = db.session.query(Printer).filter_by(is_active=True, is_online=True).count()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    alerts_today = (
        db.session.query(AlertEvent)
        .filter(AlertEvent.occurred_at >= today)
        .count()
    )

    # Locations for filter dropdown
    locations = db.session.query(Location).order_by(Location.name).all()
    location_id = request.args.get("location", type=int)

    query = db.session.query(Printer).filter_by(is_active=True)
    if location_id:
        query = query.filter_by(location_id=location_id)
    printers = query.order_by(Printer.display_name, Printer.ip_address).all()

    # Pre-fetch telemetry so initial render is fully populated (no flash)
    printer_data = _build_printer_data(printers)

    return render_template(
        "dashboard/index.html",
        printer_data=printer_data,
        total=total,
        online=online,
        offline=total - online,
        alerts_today=alerts_today,
        locations=locations,
        active_location_id=location_id,
    )


@bp.route("/health")
def health():
    return {"status": "ok"}, 200
