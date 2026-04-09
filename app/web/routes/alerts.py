from __future__ import annotations

from flask import Blueprint, render_template

from app.core.database import db
from app.models import AlertEvent, Printer

bp = Blueprint("alerts", __name__, url_prefix="/alerts")


@bp.route("/")
def log():
    events = (
        db.session.query(AlertEvent, Printer)
        .join(Printer, AlertEvent.printer_id == Printer.id)
        .order_by(AlertEvent.occurred_at.desc())
        .limit(200)
        .all()
    )
    return render_template("alerts/log.html", events=events)
