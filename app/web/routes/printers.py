from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.core.config import config
from app.core.database import db
from app.models import Printer, SiteSetting, SupplySnapshot, TelemetrySnapshot
from app.models.printer import PrinterGroup
from app.web.routes.auth import admin_required

bp = Blueprint("printers", __name__, url_prefix="/printers")


@bp.route("/")
@login_required
def list_printers():
    printers = (
        db.session.query(Printer)
        .filter_by(is_active=True)
        .order_by(Printer.display_name, Printer.ip_address)
        .all()
    )
    removed = (
        db.session.query(Printer)
        .filter_by(is_active=False)
        .order_by(Printer.display_name, Printer.ip_address)
        .all()
    )
    return render_template("printers/list.html", printers=printers, removed=removed)


@bp.route("/<int:printer_id>")
@login_required
def detail(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    latest_telemetry = (
        db.session.query(TelemetrySnapshot)
        .filter_by(printer_id=printer_id)
        .order_by(TelemetrySnapshot.polled_at.desc())
        .first()
    )
    supplies = []
    if latest_telemetry:
        supplies = (
            db.session.query(SupplySnapshot)
            .filter_by(telemetry_id=latest_telemetry.id)
            .order_by(SupplySnapshot.supply_index)
            .all()
        )
    # Check if helpdesk email is configured
    helpdesk_row = db.session.get(SiteSetting, "helpdesk_email")
    helpdesk_configured = bool(helpdesk_row and helpdesk_row.value)

    return render_template(
        "printers/detail.html",
        printer=printer,
        latest_telemetry=latest_telemetry,
        supplies=supplies,
        helpdesk_configured=helpdesk_configured,
    )


@bp.route("/add", methods=["GET", "POST"])
@admin_required
def add():
    groups = db.session.query(PrinterGroup).order_by(PrinterGroup.name).all()
    if request.method == "POST":
        ip = request.form.get("ip_address", "").strip()
        display_name = request.form.get("display_name", "").strip() or None
        community = request.form.get("snmp_community", "").strip() or config.snmp.community_v2c
        notes = request.form.get("notes", "").strip() or None
        group_id = request.form.get("group_id") or None
        if group_id:
            group_id = int(group_id)

        if not ip:
            flash("IP address is required.", "danger")
            return render_template("printers/add.html", groups=groups)

        existing = db.session.query(Printer).filter_by(ip_address=ip).first()
        if existing and existing.is_active:
            flash(f"A printer with IP {ip} is already on the dashboard.", "warning")
            return redirect(url_for("printers.detail", printer_id=existing.id))

        if existing and not existing.is_active:
            existing.is_active = True
            if display_name:
                existing.display_name = display_name
            if notes:
                existing.notes = notes
            existing.snmp_community = community
            existing.group_id = group_id
            db.session.commit()
            printer = existing
            flash(f"Printer {ip} restored to the dashboard.", "success")
        else:
            printer = Printer(
                ip_address=ip,
                display_name=display_name,
                snmp_community=community,
                notes=notes,
                group_id=group_id,
            )
            db.session.add(printer)
            db.session.commit()
            flash(f"Printer {ip} added successfully.", "success")

        try:
            from app.core.database import get_db
            from app.scanner.poller import poll_single_printer
            with get_db() as sess:
                poll_single_printer(printer.id, sess)
        except Exception:
            pass

        return redirect(url_for("printers.detail", printer_id=printer.id))

    return render_template("printers/add.html", groups=groups)


@bp.route("/<int:printer_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    groups = db.session.query(PrinterGroup).order_by(PrinterGroup.name).all()
    if request.method == "POST":
        printer.display_name = request.form.get("display_name", "").strip() or None
        printer.snmp_community = request.form.get("snmp_community", "").strip() or "public"
        printer.notes = request.form.get("notes", "").strip() or None
        group_id = request.form.get("group_id") or None
        printer.group_id = int(group_id) if group_id else None
        db.session.commit()
        flash("Printer updated.", "success")
        return redirect(url_for("printers.detail", printer_id=printer.id))
    return render_template("printers/edit.html", printer=printer, groups=groups)


@bp.route("/<int:printer_id>/delete", methods=["POST"])
@admin_required
def delete(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    printer.is_active = False
    db.session.commit()
    flash(f"Printer {printer.effective_name} removed.", "success")
    return redirect(url_for("printers.list_printers"))


@bp.route("/removed")
@login_required
def removed():
    printers = (
        db.session.query(Printer)
        .filter_by(is_active=False)
        .order_by(Printer.display_name, Printer.ip_address)
        .all()
    )
    return render_template("printers/removed.html", printers=printers)


@bp.route("/<int:printer_id>/restore", methods=["POST"])
@admin_required
def restore(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    printer.is_active = True
    db.session.commit()

    try:
        from app.core.database import get_db
        from app.scanner.poller import poll_single_printer
        with get_db() as sess:
            poll_single_printer(printer_id, sess)
    except Exception:
        pass

    flash(f"Printer {printer.effective_name} restored to the dashboard.", "success")
    return redirect(url_for("printers.detail", printer_id=printer_id))


@bp.route("/<int:printer_id>/poll", methods=["POST"])
@admin_required
def poll_now(printer_id: int):
    db.get_or_404(Printer, printer_id)
    try:
        from app.core.database import get_db
        from app.scanner.poller import poll_single_printer
        with get_db() as sess:
            poll_single_printer(printer_id, sess)
        flash("Poll completed.", "success")
    except Exception as e:
        flash(f"Poll failed: {e}", "danger")
    return redirect(url_for("printers.detail", printer_id=printer_id))


@bp.route("/<int:printer_id>/ticket", methods=["POST"])
@login_required
def create_ticket(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    note = request.form.get("note", "").strip()

    latest_telemetry = (
        db.session.query(TelemetrySnapshot)
        .filter_by(printer_id=printer_id)
        .order_by(TelemetrySnapshot.polled_at.desc())
        .first()
    )
    supplies = []
    if latest_telemetry:
        supplies = (
            db.session.query(SupplySnapshot)
            .filter_by(telemetry_id=latest_telemetry.id)
            .order_by(SupplySnapshot.supply_index)
            .all()
        )

    from app.alerts.notifier import send_helpdesk_ticket
    ok, msg = send_helpdesk_ticket(printer, supplies, note, current_user.username)
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("printers.detail", printer_id=printer_id))
