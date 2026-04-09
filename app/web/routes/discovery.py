from __future__ import annotations

from flask import Blueprint, render_template, request

from app.core.database import db
from app.models import DiscoveryScan

bp = Blueprint("discovery", __name__, url_prefix="/discovery")


@bp.route("/")
def index():
    recent_scans = (
        db.session.query(DiscoveryScan)
        .order_by(DiscoveryScan.started_at.desc())
        .limit(20)
        .all()
    )
    return render_template("discovery/scan.html", recent_scans=recent_scans)
