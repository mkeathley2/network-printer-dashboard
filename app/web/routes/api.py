"""
HTMX partial-HTML endpoints and Chart.js JSON data endpoints.
"""
from __future__ import annotations

import threading
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from app.core.database import db, get_db
from app.models import (
    AlertEvent, DiscoveryScan, DiscoveryResult,
    Printer, SupplySnapshot, TelemetrySnapshot,
)
from app.web.routes.auth import admin_required
from app.web.routes.config import get_effective_thresholds

bp = Blueprint("api", __name__)


# ---------------------------------------------------------------------------
# HTMX partial: dashboard printer cards (auto-refreshes every 60s)
# ---------------------------------------------------------------------------
@bp.route("/htmx/printer-cards")
@login_required
def htmx_printer_cards():
    query = db.session.query(Printer).filter_by(is_active=True)
    group_id = request.args.get("group", type=int)
    if group_id:
        query = query.filter_by(group_id=group_id)
    printers = query.order_by(Printer.display_name, Printer.ip_address).all()

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
    return render_template("dashboard/_printer_card.html", printer_data=printer_data)


# ---------------------------------------------------------------------------
# HTMX partial: supply rows for printer detail page
# ---------------------------------------------------------------------------
@bp.route("/htmx/printer/<int:printer_id>/supplies")
@login_required
def htmx_printer_supplies(printer_id: int):
    latest = (
        db.session.query(TelemetrySnapshot)
        .filter_by(printer_id=printer_id)
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
    printer = db.session.get(Printer, printer_id)
    warn_pct, crit_pct = get_effective_thresholds(printer)
    return render_template("printers/_supply_row.html", supplies=supplies, telemetry=latest,
                           warn_pct=warn_pct, crit_pct=crit_pct)


# ---------------------------------------------------------------------------
# HTMX partial: recent alerts for dashboard sidebar
# ---------------------------------------------------------------------------
@bp.route("/htmx/alerts/recent")
@login_required
def htmx_recent_alerts():
    events = (
        db.session.query(AlertEvent, Printer)
        .join(Printer, AlertEvent.printer_id == Printer.id)
        .order_by(AlertEvent.occurred_at.desc())
        .limit(8)
        .all()
    )
    return render_template("alerts/_recent.html", events=events)


# ---------------------------------------------------------------------------
# HTMX: start a CIDR discovery scan (POST) — admin only
# ---------------------------------------------------------------------------
@bp.route("/htmx/discovery/start", methods=["POST"])
@admin_required
def htmx_discovery_start():
    cidr = request.form.get("cidr_range", "").strip()
    community = request.form.get("community", "").strip() or "public"

    if not cidr:
        return "<p class='text-danger'>Please enter a CIDR range.</p>", 400

    scan = DiscoveryScan(
        scan_type="cidr",
        cidr_range=cidr,
        status="running",
    )
    db.session.add(scan)
    db.session.commit()
    scan_id = scan.id

    from flask import current_app
    flask_app = current_app._get_current_object()

    def _run():
        with flask_app.app_context():
            try:
                from app.scanner.discovery import run_cidr_discovery
                with get_db() as sess:
                    run_cidr_discovery(cidr, community, scan_id, sess)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Discovery scan failed")
                with get_db() as sess:
                    s = sess.get(DiscoveryScan, scan_id)
                    if s:
                        s.status = "failed"
                        s.finished_at = datetime.utcnow()

    threading.Thread(target=_run, daemon=True).start()

    return render_template("discovery/_results_table.html", scan_id=scan_id, results=[], status="running")


# ---------------------------------------------------------------------------
# HTMX: poll discovery scan results
# ---------------------------------------------------------------------------
@bp.route("/htmx/discovery/<int:scan_id>/results")
@login_required
def htmx_discovery_results(scan_id: int):
    scan = db.session.get(DiscoveryScan, scan_id)
    if not scan:
        return "<p class='text-danger'>Scan not found.</p>", 404
    results = (
        db.session.query(DiscoveryResult)
        .filter_by(scan_id=scan_id)
        .order_by(DiscoveryResult.id)
        .all()
    )
    return render_template(
        "discovery/_results_table.html",
        scan_id=scan_id,
        scan=scan,
        results=results,
        status=scan.status,
    )


# ---------------------------------------------------------------------------
# Poll all printers now (admin only)
# ---------------------------------------------------------------------------
@bp.route("/poll-all", methods=["POST"])
@admin_required
def poll_all():
    from flask import current_app, flash, redirect, url_for
    flask_app = current_app._get_current_object()

    def _run():
        with flask_app.app_context():
            try:
                from app.scanner.poller import poll_all_printers
                with get_db() as sess:
                    poll_all_printers(sess)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Manual poll-all failed")

    threading.Thread(target=_run, daemon=True).start()
    flash("Poll started — data will refresh shortly.", "info")
    return redirect(url_for("dashboard.index"))


# ---------------------------------------------------------------------------
# Add all new printers from a completed scan
# ---------------------------------------------------------------------------
@bp.route("/discovery/<int:scan_id>/add-all", methods=["POST"])
@admin_required
def discovery_add_all(scan_id: int):
    from flask import flash, redirect, url_for
    from app.core.config import config as app_config

    results = (
        db.session.query(DiscoveryResult)
        .filter_by(scan_id=scan_id, already_known=False)
        .all()
    )

    added, skipped = 0, 0
    printer_ids = []

    for r in results:
        existing = db.session.query(Printer).filter_by(ip_address=r.ip_address).first()
        if existing and existing.is_active:
            skipped += 1
            continue
        if existing and not existing.is_active:
            existing.is_active = True
            existing.snmp_community = app_config.snmp.community_v2c
            db.session.commit()
            printer_ids.append(existing.id)
        else:
            printer = Printer(
                ip_address=r.ip_address,
                display_name=r.hostname or None,
                snmp_community=app_config.snmp.community_v2c,
            )
            db.session.add(printer)
            db.session.commit()
            printer_ids.append(printer.id)
        added += 1

    # Mark all as known now
    db.session.query(DiscoveryResult).filter_by(scan_id=scan_id, already_known=False).update({"already_known": True})
    db.session.commit()

    # Poll all newly added printers in background
    from flask import current_app
    flask_app = current_app._get_current_object()
    ids_to_poll = list(printer_ids)

    def _poll_all():
        with flask_app.app_context():
            from app.scanner.poller import poll_single_printer
            for pid in ids_to_poll:
                try:
                    with get_db() as sess:
                        poll_single_printer(pid, sess)
                except Exception:
                    pass

    threading.Thread(target=_poll_all, daemon=True).start()

    msg = f"{added} printer(s) added to the dashboard."
    if skipped:
        msg += f" {skipped} already present."
    flash(msg, "success")
    return redirect(url_for("discovery.index"))


# ---------------------------------------------------------------------------
# Chart.js JSON: supply level history
# ---------------------------------------------------------------------------
@bp.route("/api/history/<int:printer_id>/supplies")
@login_required
def api_supply_history(printer_id: int):
    rows = (
        db.session.query(SupplySnapshot)
        .filter_by(printer_id=printer_id)
        .order_by(SupplySnapshot.polled_at.asc())
        .all()
    )
    series: dict[str, dict] = {}
    for row in rows:
        key = f"{row.supply_index}"
        if key not in series:
            label = row.supply_description or f"{row.supply_color or 'Supply'} {row.supply_index}"
            series[key] = {"label": label, "color": row.supply_color, "data": []}
        if row.level_pct is not None:
            series[key]["data"].append({
                "x": row.polled_at.isoformat(),
                "y": row.level_pct,
            })
    return jsonify(list(series.values()))


# ---------------------------------------------------------------------------
# Chart.js JSON: page count history
# ---------------------------------------------------------------------------
@bp.route("/api/history/<int:printer_id>/pages")
@login_required
def api_page_history(printer_id: int):
    rows = (
        db.session.query(TelemetrySnapshot)
        .filter_by(printer_id=printer_id)
        .filter(TelemetrySnapshot.page_count.isnot(None))
        .order_by(TelemetrySnapshot.polled_at.asc())
        .all()
    )
    data = [{"x": r.polled_at.isoformat(), "y": r.page_count} for r in rows]
    return jsonify(data)
