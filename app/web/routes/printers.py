from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.core.config import config
from app.core.database import db
from app.models import Printer, PrinterImportData, SiteSetting, SupplySnapshot, TelemetrySnapshot
from app.models.location import Location
from app.utils.audit import audit
from app.web.routes.auth import admin_required
from app.web.routes.config import get_effective_thresholds

bp = Blueprint("printers", __name__, url_prefix="/printers")


def _apply_import_data(printer: Printer) -> bool:
    """
    Look up printer IP in printer_import_data. If found, fill any blank fields.
    Only fills fields that are currently empty — never overwrites manual data.
    Returns True if a match was found.
    """
    row = db.session.get(PrinterImportData, printer.ip_address)
    if not row:
        return False
    if not printer.assigned_person:
        printer.assigned_person = row.assigned_person
    if not printer.sql_number:
        printer.sql_number = row.sql_number
    if not printer.assigned_computer:
        printer.assigned_computer = row.assigned_computer
    if not printer.phone_ext:
        printer.phone_ext = row.phone_ext
    if not printer.printer_web_username:
        printer.printer_web_username = row.printer_web_username
    if not printer.printer_web_password:
        printer.printer_web_password = row.printer_web_password
    if not printer.location_id and row.location_name:
        loc = db.session.query(Location).filter_by(name=row.location_name).first()
        if loc:
            printer.location_id = loc.id
    return True


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

    warn_pct, crit_pct = get_effective_thresholds(printer)
    return render_template(
        "printers/detail.html",
        printer=printer,
        latest_telemetry=latest_telemetry,
        supplies=supplies,
        helpdesk_configured=helpdesk_configured,
        warn_pct=warn_pct,
        crit_pct=crit_pct,
    )


@bp.route("/add", methods=["GET", "POST"])
@admin_required
def add():
    locations = db.session.query(Location).order_by(Location.name).all()
    if request.method == "POST":
        ip = request.form.get("ip_address", "").strip()
        display_name = request.form.get("display_name", "").strip() or None
        community = request.form.get("snmp_community", "").strip() or config.snmp.community_v2c
        notes = request.form.get("notes", "").strip() or None
        location_id = request.form.get("location_id") or None
        if location_id:
            location_id = int(location_id)

        if not ip:
            flash("IP address is required.", "danger")
            return render_template("printers/add.html", locations=locations)

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
            existing.location_id = location_id
            _apply_import_data(existing)
            db.session.commit()
            printer = existing
            flash(f"Printer {ip} restored to the dashboard.", "success")
            audit(current_user.username, "printer_restore", ip,
                  f"Restored previously-removed printer {ip}")
        else:
            printer = Printer(
                ip_address=ip,
                display_name=display_name,
                snmp_community=community,
                notes=notes,
                location_id=location_id,
            )
            db.session.add(printer)
            db.session.flush()
            _apply_import_data(printer)
            db.session.commit()
            flash(f"Printer {ip} added successfully.", "success")
            audit(current_user.username, "printer_add", ip,
                  f"Added printer {ip}" + (f" ({display_name})" if display_name else ""))

        try:
            from app.core.database import get_db
            from app.scanner.poller import poll_single_printer
            with get_db() as sess:
                poll_single_printer(printer.id, sess)
        except Exception as poll_err:
            audit(current_user.username, "printer_poll_fail", ip,
                  f"Initial poll failed after add: {poll_err}", success=False)

        return redirect(url_for("printers.detail", printer_id=printer.id))

    return render_template("printers/add.html", locations=locations)


@bp.route("/<int:printer_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    locations = db.session.query(Location).order_by(Location.name).all()
    if request.method == "POST":
        printer.display_name = request.form.get("display_name", "").strip() or None
        printer.snmp_community = request.form.get("snmp_community", "").strip() or "public"
        printer.notes = request.form.get("notes", "").strip() or None
        location_id = request.form.get("location_id") or None
        printer.location_id = int(location_id) if location_id else None
        printer.assigned_person = request.form.get("assigned_person", "").strip() or None
        printer.sql_number = request.form.get("sql_number", "").strip() or None
        printer.assigned_computer = request.form.get("assigned_computer", "").strip() or None
        printer.phone_ext = request.form.get("phone_ext", "").strip() or None
        printer.printer_web_username = request.form.get("printer_web_username", "").strip() or None
        new_pw = request.form.get("printer_web_password", "").strip()
        if new_pw:
            printer.printer_web_password = new_pw
        db.session.commit()
        audit(current_user.username, "printer_edit", printer.ip_address,
              f"Edited printer {printer.effective_name} ({printer.ip_address})")
        flash("Printer updated.", "success")
        return redirect(url_for("printers.detail", printer_id=printer.id))
    return render_template("printers/edit.html", printer=printer, locations=locations)


@bp.route("/<int:printer_id>/delete", methods=["POST"])
@admin_required
def delete(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    name = printer.effective_name
    printer.is_active = False
    db.session.commit()
    audit(current_user.username, "printer_delete", printer.ip_address,
          f"Removed printer {name} ({printer.ip_address})")
    flash(f"Printer {name} removed.", "success")
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
    audit(current_user.username, "printer_restore", printer.ip_address,
          f"Restored printer {printer.effective_name} ({printer.ip_address})")

    try:
        from app.core.database import get_db
        from app.scanner.poller import poll_single_printer
        with get_db() as sess:
            poll_single_printer(printer_id, sess)
    except Exception:
        pass

    flash(f"Printer {printer.effective_name} restored to the dashboard.", "success")
    return redirect(url_for("printers.detail", printer_id=printer_id))


@bp.route("/<int:printer_id>/thresholds", methods=["POST"])
@admin_required
def set_thresholds(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    use_default = request.form.get("use_default") == "1"
    if use_default:
        printer.supply_warn_pct = None
        printer.supply_crit_pct = None
        db.session.commit()
        audit(current_user.username, "printer_thresholds", printer.ip_address,
              f"Reset thresholds to site default for {printer.effective_name}")
        flash("Thresholds reset to site defaults.", "success")
    else:
        try:
            warn = int(request.form.get("supply_warn_pct", 15))
            crit = int(request.form.get("supply_crit_pct", 5))
        except (ValueError, TypeError):
            flash("Invalid threshold values.", "danger")
            return redirect(url_for("printers.detail", printer_id=printer_id))
        if not (0 < crit < warn <= 100):
            flash("Warning must be greater than critical, and both between 1–99.", "danger")
            return redirect(url_for("printers.detail", printer_id=printer_id))
        printer.supply_warn_pct = warn
        printer.supply_crit_pct = crit
        db.session.commit()
        audit(current_user.username, "printer_thresholds", printer.ip_address,
              f"Set thresholds warn={warn}% crit={crit}% for {printer.effective_name}")
        flash("Printer thresholds saved.", "success")
    return redirect(url_for("printers.detail", printer_id=printer_id))


@bp.route("/<int:printer_id>/resend-alerts", methods=["POST"])
@admin_required
def resend_alerts(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    try:
        from app.core.database import get_db
        from app.models import AlertState
        from app.scanner.poller import poll_single_printer
        with get_db() as sess:
            # Clear all alert state flags so the evaluator re-sends on next poll
            sess.query(AlertState).filter_by(printer_id=printer_id).delete()
            sess.commit()
            # Immediately poll so emails go out now rather than waiting
            poll_single_printer(printer_id, sess)
        audit(current_user.username, "printer_resend_alerts", printer.ip_address,
              f"Resent alerts for {printer.effective_name}")
        flash("Alert state reset and emails resent for active alerts.", "success")
    except Exception as e:
        audit(current_user.username, "printer_resend_alerts", printer.ip_address,
              f"Resend alerts failed for {printer.effective_name}: {e}", success=False)
        flash(f"Resend failed: {e}", "danger")
    return redirect(url_for("printers.detail", printer_id=printer_id))


@bp.route("/<int:printer_id>/poll", methods=["POST"])
@admin_required
def poll_now(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    try:
        from app.core.database import get_db
        from app.scanner.poller import poll_single_printer
        with get_db() as sess:
            poll_single_printer(printer_id, sess)
        audit(current_user.username, "printer_poll", printer.ip_address,
              f"Manual poll of {printer.effective_name}")
        flash("Poll completed.", "success")
    except Exception as e:
        audit(current_user.username, "printer_poll", printer.ip_address,
              f"Manual poll failed for {printer.effective_name}: {e}", success=False)
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
    audit(current_user.username, "helpdesk_ticket", printer.ip_address,
          f"Helpdesk ticket {'sent' if ok else 'failed'} for {printer.effective_name}: {msg}",
          success=ok)
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("printers.detail", printer_id=printer_id))
