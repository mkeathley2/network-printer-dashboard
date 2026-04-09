from __future__ import annotations

from flask import Blueprint, render_template

from app.core.database import db
from app.models import AlertEvent, Printer

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    printers = db.session.query(Printer).filter_by(is_active=True).order_by(Printer.display_name, Printer.ip_address).all()
    recent_alerts = (
        db.session.query(AlertEvent)
        .order_by(AlertEvent.occurred_at.desc())
        .limit(10)
        .all()
    )
    return render_template("dashboard/index.html", printers=printers, recent_alerts=recent_alerts)


@bp.route("/health")
def health():
    return {"status": "ok"}, 200
