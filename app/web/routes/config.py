"""Admin-only configuration routes: SMTP, Users, Locations, Import."""
from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.security import generate_password_hash

from app.core.database import db
from app.models import Printer, PrinterImportData, SiteSetting, User
from app.models.location import Location
from app.models.printer import PrinterGroup
from app.utils.audit import audit
from app.web.routes.auth import admin_required

bp = Blueprint("config", __name__, url_prefix="/config")

# Setting keys stored in SiteSetting table
SMTP_KEYS = ["smtp_host", "smtp_port", "smtp_auth", "smtp_user", "smtp_password", "smtp_from", "alert_to", "helpdesk_email"]

THRESHOLD_WARN_DEFAULT = 15
THRESHOLD_CRIT_DEFAULT = 5


def get_effective_thresholds(printer=None) -> tuple[int, int]:
    """
    Return (warn_pct, crit_pct) for a printer.
    Printer-level overrides take priority; falls back to site-wide SiteSetting,
    then to hardcoded defaults.
    """
    if printer is not None:
        if printer.supply_warn_pct is not None and printer.supply_crit_pct is not None:
            return printer.supply_warn_pct, printer.supply_crit_pct

    try:
        warn = int(_get_setting("supply_warn_pct", str(THRESHOLD_WARN_DEFAULT)))
        crit = int(_get_setting("supply_crit_pct", str(THRESHOLD_CRIT_DEFAULT)))
    except (ValueError, TypeError):
        warn, crit = THRESHOLD_WARN_DEFAULT, THRESHOLD_CRIT_DEFAULT
    return warn, crit


def _get_setting(key: str, default: str = "") -> str:
    row = db.session.get(SiteSetting, key)
    return row.value if (row and row.value is not None) else default


def _set_setting(key: str, value: str) -> None:
    row = db.session.get(SiteSetting, key)
    if row:
        row.value = value
    else:
        db.session.add(SiteSetting(key=key, value=value))


# ---------------------------------------------------------------------------
# Main config page (GET)
# ---------------------------------------------------------------------------
@bp.route("/")
@admin_required
def index():
    smtp = {k: _get_setting(k) for k in SMTP_KEYS}
    users = db.session.query(User).order_by(User.username).all()
    locations = db.session.query(Location).order_by(Location.name).all()
    location_counts = {
        loc.id: db.session.query(Printer).filter_by(location_id=loc.id, is_active=True).count()
        for loc in locations
    }
    import_count = db.session.query(PrinterImportData).count()

    # Compute import record status vs active printers
    printers_by_ip = {
        p.ip_address: p
        for p in db.session.query(Printer).filter_by(is_active=True).all()
    }
    import_rows = db.session.query(PrinterImportData).order_by(PrinterImportData.ip_address).all()

    import_applied_count = 0   # on dashboard + has data applied
    import_pending_rows = []   # on dashboard + data not yet applied
    import_undiscovered_rows = []  # IP not on dashboard at all

    for r in import_rows:
        printer = printers_by_ip.get(r.ip_address)
        if printer is None:
            import_undiscovered_rows.append(r)
        elif printer.location_id or printer.assigned_person or printer.sql_number:
            import_applied_count += 1
        else:
            import_pending_rows.append(r)

    warn_pct = _get_setting("supply_warn_pct", str(THRESHOLD_WARN_DEFAULT))
    crit_pct = _get_setting("supply_crit_pct", str(THRESHOLD_CRIT_DEFAULT))

    tab = request.args.get("tab", "smtp")

    audit_entries = []
    if tab == "activity":
        from app.models.audit import AuditLog
        audit_entries = (
            db.session.query(AuditLog)
            .order_by(AuditLog.occurred_at.desc())
            .limit(500)
            .all()
        )

    return render_template(
        "config/index.html",
        smtp=smtp,
        users=users,
        locations=locations,
        location_counts=location_counts,
        import_count=import_count,
        active_tab=tab,
        warn_pct=warn_pct,
        crit_pct=crit_pct,
        audit_entries=audit_entries,
        import_applied_count=import_applied_count,
        import_pending_rows=import_pending_rows,
        import_undiscovered_rows=import_undiscovered_rows,
    )


# ---------------------------------------------------------------------------
# Threshold settings
# ---------------------------------------------------------------------------
@bp.route("/thresholds", methods=["POST"])
@admin_required
def save_thresholds():
    try:
        warn = int(request.form.get("supply_warn_pct", THRESHOLD_WARN_DEFAULT))
        crit = int(request.form.get("supply_crit_pct", THRESHOLD_CRIT_DEFAULT))
    except (ValueError, TypeError):
        flash("Invalid threshold values — must be whole numbers.", "danger")
        return redirect(url_for("config.index", tab="thresholds"))
    if not (0 < crit < warn <= 100):
        flash("Warning must be greater than critical, and both must be between 1–99.", "danger")
        return redirect(url_for("config.index", tab="thresholds"))
    _set_setting("supply_warn_pct", str(warn))
    _set_setting("supply_crit_pct", str(crit))
    db.session.commit()
    audit(current_user.username, "config_thresholds", "site",
          f"Updated site thresholds: warn={warn}%, crit={crit}%")
    flash("Threshold settings saved.", "success")
    return redirect(url_for("config.index", tab="thresholds"))


# ---------------------------------------------------------------------------
# SMTP settings
# ---------------------------------------------------------------------------
@bp.route("/smtp", methods=["POST"])
@admin_required
def save_smtp():
    for key in ["smtp_host", "smtp_port", "smtp_auth", "smtp_user", "smtp_from", "alert_to", "helpdesk_email"]:
        _set_setting(key, request.form.get(key, "").strip())
    # Only update password if a new one was supplied
    new_pw = request.form.get("smtp_password", "").strip()
    if new_pw:
        _set_setting("smtp_password", new_pw)
    db.session.commit()
    audit(current_user.username, "config_smtp", "smtp", "Updated SMTP settings")
    flash("SMTP settings saved.", "success")
    return redirect(url_for("config.index", tab="smtp"))


@bp.route("/smtp/test", methods=["POST"])
@admin_required
def test_smtp():
    from app.alerts.notifier import send_test_email
    ok, msg = send_test_email()
    audit(current_user.username, "config_smtp_test", "smtp",
          f"Test email {'succeeded' if ok else 'failed'}: {msg}", success=ok)
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("config.index", tab="smtp"))


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
@bp.route("/users/add", methods=["POST"])
@admin_required
def add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "viewer").strip()

    if not username or not password:
        flash("Username and password are required.", "danger")
        return redirect(url_for("config.index", tab="users"))

    if db.session.query(User).filter_by(username=username).first():
        flash(f"Username '{username}' already exists.", "danger")
        return redirect(url_for("config.index", tab="users"))

    db.session.add(User(
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
    ))
    db.session.commit()
    audit(current_user.username, "user_add", username, f"Created user '{username}' with role {role}")
    flash(f"User '{username}' created.", "success")
    return redirect(url_for("config.index", tab="users"))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("config.index", tab="users"))
    deleted_name = user.username
    db.session.delete(user)
    db.session.commit()
    audit(current_user.username, "user_delete", deleted_name, f"Deleted user '{deleted_name}'")
    flash(f"User '{deleted_name}' deleted.", "success")
    return redirect(url_for("config.index", tab="users"))


@bp.route("/users/<int:user_id>/set-password", methods=["POST"])
@admin_required
def set_user_password(user_id: int):
    user = db.get_or_404(User, user_id)
    new_pw = request.form.get("new_password", "").strip()
    if len(new_pw) < 4:
        flash("Password must be at least 4 characters.", "danger")
        return redirect(url_for("config.index", tab="users"))
    user.password_hash = generate_password_hash(new_pw)
    db.session.commit()
    audit(current_user.username, "user_password", user.username,
          f"Changed password for '{user.username}'")
    flash(f"Password updated for '{user.username}'.", "success")
    return redirect(url_for("config.index", tab="users"))


@bp.route("/users/<int:user_id>/set-role", methods=["POST"])
@admin_required
def set_user_role(user_id: int):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot change your own role.", "danger")
        return redirect(url_for("config.index", tab="users"))
    role = request.form.get("role", "viewer")
    if role not in ("admin", "viewer"):
        flash("Invalid role.", "danger")
        return redirect(url_for("config.index", tab="users"))
    user.role = role
    db.session.commit()
    audit(current_user.username, "user_role", user.username,
          f"Changed role for '{user.username}' to {role}")
    flash(f"Role for '{user.username}' updated to {role}.", "success")
    return redirect(url_for("config.index", tab="users"))


# ---------------------------------------------------------------------------
# Location management
# ---------------------------------------------------------------------------
@bp.route("/locations/add", methods=["POST"])
@admin_required
def add_location():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip() or None
    if not name:
        flash("Location name is required.", "danger")
        return redirect(url_for("config.index", tab="locations"))
    if db.session.query(Location).filter_by(name=name).first():
        flash(f"Location '{name}' already exists.", "danger")
        return redirect(url_for("config.index", tab="locations"))
    db.session.add(Location(name=name, description=description))
    db.session.commit()
    audit(current_user.username, "location_add", name, f"Created location '{name}'")
    flash(f"Location '{name}' created.", "success")
    return redirect(url_for("config.index", tab="locations"))


@bp.route("/locations/<int:location_id>/delete", methods=["POST"])
@admin_required
def delete_location(location_id: int):
    location = db.get_or_404(Location, location_id)
    loc_name = location.name
    db.session.query(Printer).filter_by(location_id=location_id).update({"location_id": None})
    db.session.delete(location)
    db.session.commit()
    audit(current_user.username, "location_delete", loc_name, f"Deleted location '{loc_name}'")
    flash(f"Location '{loc_name}' deleted.", "success")
    return redirect(url_for("config.index", tab="locations"))


# ---------------------------------------------------------------------------
# Spreadsheet import
# ---------------------------------------------------------------------------
@bp.route("/import-spreadsheet", methods=["POST"])
@admin_required
def import_spreadsheet():
    f = request.files.get("spreadsheet")
    if not f or not f.filename:
        flash("No file selected.", "danger")
        return redirect(url_for("config.index", tab="import"))
    if not f.filename.lower().endswith(".xlsx"):
        flash("Only .xlsx files are supported.", "danger")
        return redirect(url_for("config.index", tab="import"))

    from app.utils.spreadsheet_import import import_printer_spreadsheet
    result = import_printer_spreadsheet(f.read())

    if result["errors"]:
        for err in result["errors"][:5]:
            flash(err, "warning")

    locs = ", ".join(result["locations"]) if result["locations"] else "none"
    audit(current_user.username, "import_spreadsheet", f.filename,
          f"Imported {result['imported']} records across {len(result['locations'])} location(s); "
          f"{result['skipped']} skipped")
    flash(
        f"Import complete: {result['imported']} printer records staged across "
        f"{len(result['locations'])} location(s) ({locs}). "
        f"{result['skipped']} rows skipped.",
        "success" if result["imported"] > 0 else "warning",
    )
    return redirect(url_for("config.index", tab="import"))


@bp.route("/apply-import-data", methods=["POST"])
@admin_required
def apply_import_data():
    """Apply staged import data to all existing active printers that are missing fields."""
    from app.web.routes.printers import _apply_import_data

    printers = db.session.query(Printer).filter_by(is_active=True).all()
    updated = 0
    for printer in printers:
        changed = _apply_import_data(printer)
        if changed:
            updated += 1
    db.session.commit()
    audit(current_user.username, "import_apply_all", "all printers",
          f"Applied import data to {updated} existing printer(s)")
    flash(f"Import data applied to {updated} printer(s).", "success" if updated else "info")
    return redirect(url_for("config.index", tab="import"))


# ---------------------------------------------------------------------------
# Activity log CSV export
# ---------------------------------------------------------------------------
@bp.route("/activity/export")
@admin_required
def export_activity_log():
    from app.models.audit import AuditLog
    rows = (
        db.session.query(AuditLog)
        .order_by(AuditLog.occurred_at.desc())
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "username", "action", "target", "detail", "success"])
    for r in rows:
        writer.writerow([
            r.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
            r.username,
            r.action,
            r.target or "",
            r.detail or "",
            "yes" if r.success else "no",
        ])
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=activity_log.csv"},
    )
