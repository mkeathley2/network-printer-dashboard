"""
Reports blueprint — accessible to all logged-in users.
Provides print volume, toner cost, consumption rate, reliability, and cost-per-page reports.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Optional

from flask import Blueprint, Response, render_template, request
from flask_login import login_required

from app.core.database import db
from app.models import Printer, TelemetrySnapshot, SupplySnapshot
from app.models.alert import AlertEvent
from app.models.location import Location
from app.utils.depletion import compute_supply_depletion
from app.utils.regression import linear_regression

logger = logging.getLogger(__name__)

bp = Blueprint("reports", __name__, url_prefix="/reports")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _date_range(days_param: str) -> tuple[Optional[datetime], datetime]:
    """Return (start_dt, end_dt) based on the 'days' query param."""
    now = datetime.utcnow()
    try:
        days = int(days_param)
    except (TypeError, ValueError):
        days = 90
    if days <= 0:
        return None, now  # "All time"
    return now - timedelta(days=days), now


def _all_active_printers():
    return (
        db.session.query(Printer)
        .filter_by(is_active=True)
        .order_by(Printer.display_name, Printer.ip_address)
        .all()
    )


# Linear regression now lives in app.utils.regression (imported above) so the
# history page and the predictive-toner scheduler can share the same helper.
_linear_regression = linear_regression  # kept for backward compatibility


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    return render_template("reports/index.html")


# ---------------------------------------------------------------------------
# Report 1: Print Volume
# ---------------------------------------------------------------------------

@bp.route("/print-volume")
@login_required
def print_volume():
    days = request.args.get("days", "90")
    group_by = request.args.get("group_by", "printer")
    fmt = request.args.get("format", "html")

    start_dt, end_dt = _date_range(days)
    printers = _all_active_printers()

    rows = []
    for p in printers:
        q = db.session.query(TelemetrySnapshot).filter(
            TelemetrySnapshot.printer_id == p.id,
            TelemetrySnapshot.page_count.isnot(None),
        )
        if start_dt:
            q = q.filter(TelemetrySnapshot.polled_at >= start_dt)
        q = q.filter(TelemetrySnapshot.polled_at <= end_dt).order_by(TelemetrySnapshot.polled_at)
        snaps = q.all()
        if len(snaps) < 2:
            continue
        start_count = snaps[0].page_count
        end_count = snaps[-1].page_count
        delta = max(0, end_count - start_count)
        if delta == 0:
            continue
        rows.append({
            "printer_id": p.id,
            "printer_name": p.effective_name,
            "location": p.location.name if p.location else "",
            "assigned_person": p.assigned_person or "",
            "pages_printed": delta,
            "start_count": start_count,
            "end_count": end_count,
        })

    # Group if needed
    if group_by == "person":
        groups: dict[str, int] = {}
        for r in rows:
            key = r["assigned_person"] or "Unassigned"
            groups[key] = groups.get(key, 0) + r["pages_printed"]
        display_rows = [{"label": k, "pages_printed": v} for k, v in sorted(groups.items(), key=lambda x: -x[1])]
    elif group_by == "location":
        groups = {}
        for r in rows:
            key = r["location"] or "No Location"
            groups[key] = groups.get(key, 0) + r["pages_printed"]
        display_rows = [{"label": k, "pages_printed": v} for k, v in sorted(groups.items(), key=lambda x: -x[1])]
    else:
        rows.sort(key=lambda r: -r["pages_printed"])
        display_rows = [{"label": r["printer_name"], "pages_printed": r["pages_printed"], **r} for r in rows]

    if fmt == "csv":
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(["Printer", "Location", "Person", "Pages Printed", "Start Count", "End Count"])
        for r in rows:
            writer.writerow([r["printer_name"], r["location"], r["assigned_person"],
                             r["pages_printed"], r["start_count"], r["end_count"]])
        return Response(si.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=print_volume.csv"})

    return render_template("reports/print_volume.html",
                           rows=rows, display_rows=display_rows,
                           days=days, group_by=group_by)


# ---------------------------------------------------------------------------
# Report 2: Page Count Over Time (JSON API + page)
# ---------------------------------------------------------------------------

@bp.route("/prints-over-time")
@login_required
def prints_over_time():
    days = request.args.get("days", "90")
    fmt = request.args.get("format", "html")
    start_dt, end_dt = _date_range(days)
    printers = _all_active_printers()

    if fmt == "csv":
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(["Date", "Printer", "Page Count"])
        for p in printers:
            q = db.session.query(TelemetrySnapshot).filter(
                TelemetrySnapshot.printer_id == p.id,
                TelemetrySnapshot.page_count.isnot(None),
            )
            if start_dt:
                q = q.filter(TelemetrySnapshot.polled_at >= start_dt)
            snaps = q.order_by(TelemetrySnapshot.polled_at).all()
            for s in snaps:
                writer.writerow([s.polled_at.strftime("%Y-%m-%d %H:%M"), p.effective_name, s.page_count])
        return Response(si.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=prints_over_time.csv"})

    return render_template("reports/prints_over_time.html", printers=printers, days=days)


@bp.route("/api/pages-over-time")
@login_required
def api_pages_over_time():
    """JSON endpoint: returns {printer_id: {name, data: [{x, y}]}} for all printers."""
    import json as _json
    days = request.args.get("days", "90")
    printer_ids = request.args.getlist("p", type=int)
    start_dt, end_dt = _date_range(days)
    printers = _all_active_printers()
    if printer_ids:
        printers = [p for p in printers if p.id in printer_ids]

    result = {}
    for p in printers:
        q = db.session.query(TelemetrySnapshot).filter(
            TelemetrySnapshot.printer_id == p.id,
            TelemetrySnapshot.page_count.isnot(None),
        )
        if start_dt:
            q = q.filter(TelemetrySnapshot.polled_at >= start_dt)
        snaps = q.order_by(TelemetrySnapshot.polled_at).all()
        if not snaps:
            continue
        result[str(p.id)] = {
            "name": p.effective_name,
            "data": [{"x": s.polled_at.strftime("%Y-%m-%dT%H:%M:%S"), "y": s.page_count} for s in snaps],
        }

    from flask import jsonify
    return jsonify(result)


# ---------------------------------------------------------------------------
# Report 3: Toner Cost
# ---------------------------------------------------------------------------

@bp.route("/toner-cost")
@login_required
def toner_cost():
    days = request.args.get("days", "365")
    fmt = request.args.get("format", "html")
    start_dt, end_dt = _date_range(days)

    q = db.session.query(AlertEvent, Printer).join(
        Printer, AlertEvent.printer_id == Printer.id
    ).filter(
        AlertEvent.event_type.in_(["toner_replaced", "drum_replaced"]),
        Printer.is_active == True,
    )
    if start_dt:
        q = q.filter(AlertEvent.occurred_at >= start_dt)
    q = q.filter(AlertEvent.occurred_at <= end_dt).order_by(AlertEvent.occurred_at.desc())
    events = q.all()

    rows = []
    for evt, printer in events:
        rows.append({
            "event_id": evt.id,
            "occurred_at": evt.occurred_at,
            "printer_name": printer.effective_name,
            "printer_id": printer.id,
            "location": printer.location.name if printer.location else "",
            "color": evt.supply_color or "unknown",
            "event_type": evt.event_type,
            "level_pct": evt.level_pct_at_event,
            "cost": float(evt.replacement_cost) if evt.replacement_cost is not None else None,
        })

    # Totals by printer (only rows with cost)
    totals: dict[str, float] = {}
    for r in rows:
        if r["cost"] is not None:
            totals[r["printer_name"]] = totals.get(r["printer_name"], 0.0) + r["cost"]
    totals_sorted = sorted(totals.items(), key=lambda x: -x[1])

    if fmt == "csv":
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(["Date", "Printer", "Location", "Color", "Type", "Level at Replace", "Cost"])
        for r in rows:
            writer.writerow([
                r["occurred_at"].strftime("%Y-%m-%d %H:%M"),
                r["printer_name"], r["location"], r["color"],
                r["event_type"], r["level_pct"],
                f"${r['cost']:.2f}" if r["cost"] is not None else "",
            ])
        return Response(si.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=toner_cost.csv"})

    return render_template("reports/toner_cost.html",
                           rows=rows, totals=totals_sorted, days=days)


# ---------------------------------------------------------------------------
# Report 4: Cost Per Page
# ---------------------------------------------------------------------------

@bp.route("/cost-per-page")
@login_required
def cost_per_page():
    days = request.args.get("days", "365")
    fmt = request.args.get("format", "html")
    start_dt, end_dt = _date_range(days)

    printers = _all_active_printers()
    rows = []

    for p in printers:
        # Total toner cost
        q_cost = db.session.query(AlertEvent).filter(
            AlertEvent.printer_id == p.id,
            AlertEvent.event_type.in_(["toner_replaced", "drum_replaced"]),
            AlertEvent.replacement_cost.isnot(None),
        )
        if start_dt:
            q_cost = q_cost.filter(AlertEvent.occurred_at >= start_dt)
        cost_events = q_cost.all()
        total_cost = sum(float(e.replacement_cost) for e in cost_events)
        if total_cost == 0:
            continue

        # Pages printed in same period
        q_pages = db.session.query(TelemetrySnapshot).filter(
            TelemetrySnapshot.printer_id == p.id,
            TelemetrySnapshot.page_count.isnot(None),
        )
        if start_dt:
            q_pages = q_pages.filter(TelemetrySnapshot.polled_at >= start_dt)
        snaps = q_pages.order_by(TelemetrySnapshot.polled_at).all()
        if len(snaps) < 2:
            continue
        pages = max(0, snaps[-1].page_count - snaps[0].page_count)
        if pages == 0:
            continue

        rows.append({
            "printer_name": p.effective_name,
            "location": p.location.name if p.location else "",
            "total_cost": total_cost,
            "total_pages": pages,
            "cost_per_page": total_cost / pages,
            "replacements": len(cost_events),
        })

    rows.sort(key=lambda r: -r["cost_per_page"])

    if fmt == "csv":
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(["Printer", "Location", "Total Cost", "Pages Printed", "Cost Per Page", "Replacements"])
        for r in rows:
            writer.writerow([
                r["printer_name"], r["location"],
                f"${r['total_cost']:.2f}", r["total_pages"],
                f"${r['cost_per_page']:.4f}", r["replacements"],
            ])
        return Response(si.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=cost_per_page.csv"})

    return render_template("reports/cost_per_page.html", rows=rows, days=days)


# ---------------------------------------------------------------------------
# Report 5: Toner Consumption Rate
# ---------------------------------------------------------------------------

@bp.route("/consumption-rate")
@login_required
def consumption_rate():
    days = request.args.get("days", "30")
    fmt = request.args.get("format", "html")
    try:
        window_days = int(days)
    except (TypeError, ValueError):
        window_days = 30
    if window_days <= 0:
        window_days = None  # all-time

    printers = _all_active_printers()
    rows = []

    for p in printers:
        # Find every distinct supply_index this printer has seen for tonerCartridges
        indexes = [
            row[0] for row in db.session.query(SupplySnapshot.supply_index)
            .filter(
                SupplySnapshot.printer_id == p.id,
                SupplySnapshot.supply_type == "tonerCartridge",
                SupplySnapshot.level_pct.isnot(None),
            )
            .distinct()
            .all()
        ]

        for idx in indexes:
            try:
                # Replacement-aware regression: only fits to data since the last
                # toner_replaced/drum_replaced event for this slot.
                d = compute_supply_depletion(p.id, idx, db.session, window_days=window_days)
                if not d or d["slope_pct_per_day"] >= 0:
                    continue  # not depleting / insufficient data

                # Pull the latest snapshot to get the current color/description
                latest = (
                    db.session.query(SupplySnapshot)
                    .filter_by(printer_id=p.id, supply_index=idx)
                    .order_by(SupplySnapshot.polled_at.desc())
                    .first()
                )
                color = (latest.supply_color if latest else "unknown") or "unknown"
                desc = (latest.supply_description if latest else "") or f"{color.title()} Toner"

                rows.append({
                    "printer_name": p.effective_name,
                    "printer_id": p.id,
                    "location": p.location.name if p.location else "",
                    "color": color,
                    "description": desc,
                    "supply_index": idx,
                    "pct_per_day": abs(d["slope_pct_per_day"]),
                    "current_pct": d["current_pct"],
                    "days_remaining": d["days_remaining"],
                    "data_points": d["data_points"],
                })
            except Exception:
                logger.exception(
                    "Consumption-rate row failed for printer_id=%s supply_index=%s",
                    p.id, idx,
                )
                # Skip this one row, keep building the report
                continue

    rows.sort(key=lambda r: r["days_remaining"] if r["days_remaining"] is not None else 9999)

    if fmt == "csv":
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(["Printer", "Location", "Supply", "Color", "Current %",
                         "% Per Day", "Est. Days Remaining", "Data Points"])
        for r in rows:
            writer.writerow([
                r["printer_name"], r["location"], r["description"], r["color"],
                f"{r['current_pct']:.1f}", f"{r['pct_per_day']:.2f}",
                f"{r['days_remaining']:.1f}" if r["days_remaining"] else "—",
                r["data_points"],
            ])
        return Response(si.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=consumption_rate.csv"})

    return render_template("reports/consumption_rate.html", rows=rows, days=days)


# ---------------------------------------------------------------------------
# Report 6: Printer Reliability
# ---------------------------------------------------------------------------

@bp.route("/reliability")
@login_required
def reliability():
    days = request.args.get("days", "90")
    fmt = request.args.get("format", "html")
    start_dt, end_dt = _date_range(days)

    printers = _all_active_printers()
    rows = []

    for p in printers:
        q = db.session.query(AlertEvent).filter(
            AlertEvent.printer_id == p.id,
            AlertEvent.event_type == "printer_offline",
        )
        if start_dt:
            q = q.filter(AlertEvent.occurred_at >= start_dt)
        count = q.count()
        rows.append({
            "printer_name": p.effective_name,
            "printer_id": p.id,
            "location": p.location.name if p.location else "",
            "assigned_person": p.assigned_person or "",
            "offline_count": count,
            "is_online": p.is_online,
        })

    rows.sort(key=lambda r: -r["offline_count"])

    if fmt == "csv":
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(["Printer", "Location", "Person", "Offline Events", "Currently Online"])
        for r in rows:
            writer.writerow([r["printer_name"], r["location"], r["assigned_person"],
                             r["offline_count"], "Yes" if r["is_online"] else "No"])
        return Response(si.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=reliability.csv"})

    return render_template("reports/reliability.html", rows=rows, days=days)
