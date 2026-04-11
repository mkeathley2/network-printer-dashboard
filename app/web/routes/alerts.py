from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import login_required

from app.core.database import db
from app.models import AlertEvent, Printer
from app.models.alert import AlertState
from app.web.routes.auth import admin_required

bp = Blueprint("alerts", __name__, url_prefix="/alerts")


@bp.route("/")
@login_required
def log():
    events = (
        db.session.query(AlertEvent, Printer)
        .join(Printer, AlertEvent.printer_id == Printer.id)
        .order_by(AlertEvent.occurred_at.desc())
        .limit(200)
        .all()
    )
    return render_template("alerts/log.html", events=events)


@bp.route("/clear-all", methods=["POST"])
@admin_required
def clear_all():
    db.session.query(AlertState).delete()
    db.session.query(AlertEvent).delete()
    db.session.commit()
    flash("Alert history cleared. Fresh alerts will appear after the next poll.", "success")
    return redirect(url_for("alerts.log"))
