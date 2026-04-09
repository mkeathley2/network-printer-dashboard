from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, render_template, request
from flask_login import login_required

from app.core.database import db
from app.models import AlertEvent, Printer
from app.models.printer import PrinterGroup

bp = Blueprint("dashboard", __name__)


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

    # Groups for filter dropdown
    groups = db.session.query(PrinterGroup).order_by(PrinterGroup.name).all()
    group_id = request.args.get("group", type=int)

    printers = (
        db.session.query(Printer)
        .filter_by(is_active=True)
        .order_by(Printer.display_name, Printer.ip_address)
        .all()
    )

    return render_template(
        "dashboard/index.html",
        printers=printers,
        total=total,
        online=online,
        offline=total - online,
        alerts_today=alerts_today,
        groups=groups,
        active_group_id=group_id,
    )


@bp.route("/health")
def health():
    return {"status": "ok"}, 200
