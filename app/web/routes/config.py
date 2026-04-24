"""Admin-only configuration routes: SMTP, Users, Locations, Import."""
from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
import threading

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

logger = logging.getLogger(__name__)
from flask_login import current_user
from werkzeug.security import generate_password_hash

from app.core.database import db
from app.models import Printer, PrinterImportData, SiteSetting, User
from app.models.location import Location
from app.models.printer import PrinterGroup
from app.models.remote_agent import RemoteAgent
from app.utils.audit import audit
from app.utils.timezone import TIMEZONE_CHOICES
from app.web.routes.auth import admin_required

bp = Blueprint("config", __name__, url_prefix="/config")

# Setting keys stored in SiteSetting table
SMTP_KEYS = ["smtp_host", "smtp_port", "smtp_auth", "smtp_user", "smtp_password", "smtp_from", "alert_to", "helpdesk_email"]

THRESHOLD_WARN_DEFAULT = 15
THRESHOLD_CRIT_DEFAULT = 5
POLL_INTERVAL_DEFAULT = 60

ALERT_TOGGLE_DEFS = [
    ("alert_printer_offline",  "Printer Offline"),
    ("alert_printer_online",   "Printer Back Online"),
    ("alert_toner_warning",    "Toner Low Warning"),
    ("alert_toner_critical",   "Toner Critically Low"),
    ("alert_toner_replaced",   "Toner Replaced"),
    ("alert_drum_warning",     "Drum Life Low Warning"),
    ("alert_drum_critical",    "Drum Life Critically Low"),
    ("alert_drum_replaced",    "Drum Unit Replaced"),
]


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
    removed_count = db.session.query(Printer).filter_by(is_active=False).count()

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
    poll_interval = _get_setting("poll_interval_minutes", str(POLL_INTERVAL_DEFAULT))

    timezone = _get_setting("timezone", "America/Chicago")

    tab = request.args.get("tab", "smtp")

    removed_printers = []
    if tab == "removed":
        removed_printers = (
            db.session.query(Printer)
            .filter_by(is_active=False)
            .order_by(Printer.display_name, Printer.ip_address)
            .all()
        )

    audit_entries = []
    if tab == "activity":
        from app.models.audit import AuditLog
        audit_entries = (
            db.session.query(AuditLog)
            .order_by(AuditLog.occurred_at.desc())
            .limit(500)
            .all()
        )

    alert_settings = {key: _get_setting(key, "1") for key, _ in ALERT_TOGGLE_DEFS}

    # Predictive toner settings
    predictive_settings = {
        "enabled": _get_setting("predictive_toner_enabled", "0") == "1",
        "days": int(_get_setting("predictive_toner_days", "7")),
        "min_points": int(_get_setting("predictive_toner_min_points", "5")),
    }

    # Updates tab (current_version always loaded — also used by agents tab for version badge)
    from app.utils.version import get_current_version, get_latest_release, update_available
    current_version = get_current_version()
    latest_release = None
    has_update = False
    if tab == "updates":
        latest_release = get_latest_release()
        has_update = update_available()

    # Backup tab
    backup_stats = None
    if tab == "backup":
        from app.utils.backup import get_backup_stats
        backup_stats = get_backup_stats()

    # Remote Agents — always load for nav badge; full detail only needed on agents tab
    from flask import session as flask_session
    agents = db.session.query(RemoteAgent).order_by(RemoteAgent.name).all()
    public_url = _get_setting("public_url", "")
    new_agent_info = flask_session.pop("new_agent_info", None) if tab == "agents" else None

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
        poll_interval=poll_interval,
        timezone=timezone,
        timezone_choices=TIMEZONE_CHOICES,
        audit_entries=audit_entries,
        import_applied_count=import_applied_count,
        import_pending_rows=import_pending_rows,
        import_undiscovered_rows=import_undiscovered_rows,
        removed_count=removed_count,
        removed_printers=removed_printers,
        alert_settings=alert_settings,
        alert_toggle_defs=ALERT_TOGGLE_DEFS,
        predictive_settings=predictive_settings,
        current_version=current_version,
        latest_release=latest_release,
        has_update=has_update,
        backup_stats=backup_stats,
        agents=agents,
        public_url=public_url,
        new_agent_info=new_agent_info,
    )


# ---------------------------------------------------------------------------
# Alert type toggles
# ---------------------------------------------------------------------------
@bp.route("/save-alert-settings", methods=["POST"])
@admin_required
def save_alert_settings():
    for key, _ in ALERT_TOGGLE_DEFS:
        val = "1" if request.form.get(key) else "0"
        _set_setting(key, val)
    db.session.commit()
    audit(current_user.username, "config_alerts", "site", "Updated alert email settings")
    flash("Alert settings saved.", "success")
    return redirect(url_for("config.index", tab="alerts"))


@bp.route("/save-predictive-settings", methods=["POST"])
@admin_required
def save_predictive_settings():
    enabled = "1" if request.form.get("predictive_toner_enabled") else "0"
    try:
        days = max(1, min(60, int(request.form.get("predictive_toner_days", 7))))
    except (ValueError, TypeError):
        days = 7
    try:
        min_points = max(3, min(30, int(request.form.get("predictive_toner_min_points", 5))))
    except (ValueError, TypeError):
        min_points = 5
    _set_setting("predictive_toner_enabled", enabled)
    _set_setting("predictive_toner_days", str(days))
    _set_setting("predictive_toner_min_points", str(min_points))
    db.session.commit()
    audit(current_user.username, "config_predictive", "site",
          f"Predictive toner alerts {'enabled' if enabled == '1' else 'disabled'}, "
          f"threshold={days}d, min_points={min_points}")
    flash("Predictive toner settings saved.", "success")
    return redirect(url_for("config.index", tab="alerts"))


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
# Timezone setting
# ---------------------------------------------------------------------------
@bp.route("/timezone", methods=["POST"])
@admin_required
def save_timezone():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    tz = request.form.get("timezone", "America/Chicago").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, Exception):
        flash(f"Unknown timezone '{tz}'.", "danger")
        return redirect(url_for("config.index", tab="smtp"))
    _set_setting("timezone", tz)
    db.session.commit()
    audit(current_user.username, "config_timezone", "site", f"Set timezone to {tz}")
    flash(f"Timezone set to {tz}.", "success")
    return redirect(url_for("config.index", tab="smtp"))


# ---------------------------------------------------------------------------
# Poll interval setting
# ---------------------------------------------------------------------------
@bp.route("/poll-interval", methods=["POST"])
@admin_required
def save_poll_interval():
    try:
        minutes = int(request.form.get("poll_interval_minutes", POLL_INTERVAL_DEFAULT))
    except (ValueError, TypeError):
        flash("Invalid interval — must be a whole number of minutes.", "danger")
        return redirect(url_for("config.index", tab="thresholds"))
    if not (1 <= minutes <= 1440):
        flash("Interval must be between 1 and 1440 minutes (24 hours).", "danger")
        return redirect(url_for("config.index", tab="thresholds"))
    _set_setting("poll_interval_minutes", str(minutes))
    db.session.commit()
    # Reschedule the live job without restarting the server
    try:
        from app.core.extensions import scheduler
        scheduler.reschedule_job("poll_job", trigger="interval", minutes=minutes)
    except Exception:
        pass  # Scheduler not running (e.g. dev/test mode) — setting still saved
    audit(current_user.username, "config_poll_interval", "scheduler",
          f"Set poll interval to {minutes} minutes")
    flash(f"Poll interval updated to every {minutes} minutes.", "success")
    return redirect(url_for("config.index", tab="thresholds"))


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


# ---------------------------------------------------------------------------
# In-app update
# ---------------------------------------------------------------------------
@bp.route("/apply-update", methods=["POST"])
@admin_required
def apply_update():
    """
    Trigger a git pull + docker compose up --build -d in a background thread.
    Requires /var/run/docker.sock and /project/repo to be mounted in the container.
    The container will stop and restart; the browser is redirected to a progress page.
    """
    if not os.path.exists("/var/run/docker.sock"):
        flash(
            "Docker socket is not mounted. Add the volumes below to docker-compose.yml "
            "and recreate the container before using in-app updates.",
            "danger",
        )
        return redirect(url_for("config.index", tab="updates"))

    if not os.path.isdir("/project/repo/.git"):
        flash(
            "Project directory is not mounted at /project/repo. "
            "Add '.:/project/repo' to docker-compose.yml volumes.",
            "danger",
        )
        return redirect(url_for("config.index", tab="updates"))

    def _do_update() -> None:
        import time
        try:
            logger.info("Update started: running git pull…")
            subprocess.run(
                ["git", "-C", "/project/repo", "pull", "--ff-only", "origin", "master"],
                timeout=60,
                check=True,
                capture_output=True,
            )
            logger.info("git pull complete; rebuilding container…")
            time.sleep(1)
            subprocess.run(
                ["docker", "compose", "-f", "/project/repo/docker-compose.yml",
                 "up", "--build", "-d"],
                timeout=300,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.error("Update failed: %s\n%s", exc, exc.stderr)
        except Exception as exc:
            logger.error("Update failed unexpectedly: %s", exc)

    audit(current_user.username, "app_update", "system", "Triggered in-app update")
    threading.Thread(target=_do_update, daemon=True).start()
    return redirect(url_for("config.update_progress"))


@bp.route("/update-progress")
@admin_required
def update_progress():
    """Shown while the container is rebuilding. JS polls /health and redirects when back."""
    return render_template("config/update_progress.html")


# ---------------------------------------------------------------------------
# Backup / Restore / Reset
# ---------------------------------------------------------------------------
@bp.route("/backup")
@admin_required
def backup():
    """Stream a zip backup of the database."""
    scope = request.args.get("scope", "config")
    if scope not in ("config", "full"):
        scope = "config"
    from app.utils.backup import export_zip
    from datetime import datetime
    zip_bytes = export_zip(scope)
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    filename = f"printer-dashboard-backup-{scope}-{timestamp}.zip"
    audit(current_user.username, "backup_export", scope,
          f"Exported {scope} backup ({len(zip_bytes) // 1024} KB)")
    return Response(
        zip_bytes,
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.route("/restore", methods=["POST"])
@admin_required
def restore():
    """Restore the database from an uploaded backup zip."""
    f = request.files.get("backup_file")
    if not f or not f.filename:
        flash("No file selected.", "danger")
        return redirect(url_for("config.index", tab="backup"))
    if not f.filename.lower().endswith(".zip"):
        flash("Only .zip backup files are supported.", "danger")
        return redirect(url_for("config.index", tab="backup"))

    from app.utils.backup import import_zip
    try:
        result = import_zip(f.read())
    except ValueError as exc:
        flash(f"Invalid backup file: {exc}", "danger")
        return redirect(url_for("config.index", tab="backup"))
    except RuntimeError as exc:
        flash(f"Restore failed: {exc}", "danger")
        return redirect(url_for("config.index", tab="backup"))

    audit(current_user.username, "backup_restore", result.get("scope", "?"),
          f"Restored {result['rows_restored']} rows across {len(result['tables_restored'])} tables "
          f"from backup dated {result.get('backup_date', '?')}")
    flash(
        f"Restore complete — {result['rows_restored']:,} rows loaded across "
        f"{len(result['tables_restored'])} tables. "
        f"Backup scope: {result.get('scope', '?')}, "
        f"created: {result.get('backup_date', '?')[:10]}.",
        "success",
    )
    return redirect(url_for("config.index", tab="backup"))


@bp.route("/reset", methods=["POST"])
@admin_required
def reset():
    """Execute a granular factory reset based on selected categories."""
    categories = request.form.getlist("categories")
    if not categories:
        flash("No categories selected — nothing was reset.", "warning")
        return redirect(url_for("config.index", tab="backup"))

    from app.utils.backup import execute_reset
    try:
        cleared = execute_reset(categories)
    except Exception as exc:
        logger.exception("Reset failed")
        flash(f"Reset failed: {exc}", "danger")
        return redirect(url_for("config.index", tab="backup"))

    audit(current_user.username, "factory_reset", ",".join(categories),
          f"Reset: {'; '.join(cleared)}")
    flash(f"Reset complete: {', '.join(cleared)}.", "success")

    # If users were wiped, the current session is now invalid — force logout
    if "users" in categories:
        from flask_login import logout_user
        logout_user()
        return redirect(url_for("auth.login"))

    return redirect(url_for("dashboard.index"))


# ---------------------------------------------------------------------------
# Public URL setting
# ---------------------------------------------------------------------------
@bp.route("/public-url", methods=["POST"])
@admin_required
def save_public_url():
    url = request.form.get("public_url", "").strip().rstrip("/")
    _set_setting("public_url", url)
    db.session.commit()
    audit(current_user.username, "config_public_url", "site", f"Set public URL to {url}")
    flash("Public URL saved.", "success")
    return redirect(url_for("config.index", tab="agents"))


# ---------------------------------------------------------------------------
# Remote Agents management
# ---------------------------------------------------------------------------
@bp.route("/agents/add", methods=["POST"])
@admin_required
def add_agent():
    import hashlib
    import secrets
    from flask import session as flask_session

    name = request.form.get("name", "").strip()
    subnet = request.form.get("subnet", "").strip()
    try:
        scan_interval = max(1, int(request.form.get("scan_interval_minutes", 60)))
    except (ValueError, TypeError):
        scan_interval = 60

    if not name:
        flash("Agent name is required.", "danger")
        return redirect(url_for("config.index", tab="agents"))

    if db.session.query(RemoteAgent).filter_by(name=name).first():
        flash(f"Agent '{name}' already exists.", "danger")
        return redirect(url_for("config.index", tab="agents"))

    plaintext_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()

    # Auto-create a Location with the same name as the agent
    loc = db.session.query(Location).filter(Location.name.ilike(name)).first()
    if not loc:
        loc = Location(name=name)
        db.session.add(loc)
        db.session.flush()
    loc_id = loc.id
    loc_name = loc.name

    agent = RemoteAgent(
        name=name,
        location_id=loc_id,
        api_key_hash=key_hash,
        subnet=subnet or None,
        scan_interval_minutes=scan_interval,
        status="active",
    )
    db.session.add(agent)
    db.session.commit()

    # Build install commands with pre-filled values
    public_url = _get_setting("public_url", "https://your-dashboard-url.com")

    subnet_hint = subnet or "192.168.1.0/24"

    windows_cmd = (
        f'$env:AGENT_URL="{public_url}"; $env:AGENT_KEY="{plaintext_key}"; '
        f'$env:AGENT_SUBNET="{subnet_hint}"; $env:AGENT_LOCATION="{loc_name}"; '
        f'irm -Headers @{{"X-Agent-Key"=$env:AGENT_KEY}} "$env:AGENT_URL/api/agent/download/install_windows.ps1" | iex'
    )
    pi_cmd = (
        f'AGENT_URL="{public_url}" AGENT_KEY="{plaintext_key}" '
        f'AGENT_SUBNET="{subnet_hint}" AGENT_LOCATION="{loc_name}" '
        f'bash <(curl -sSL -H "X-Agent-Key: {plaintext_key}" "{public_url}/api/agent/download/install_pi.sh")'
    )

    # Store in session so the template can display it once after redirect
    flask_session["new_agent_info"] = {
        "agent_name": name,
        "plaintext_key": plaintext_key,
        "windows_cmd": windows_cmd,
        "pi_cmd": pi_cmd,
        "agent_id": agent.id,
    }

    audit(current_user.username, "agent_add", name, f"Created remote agent '{name}'")
    return redirect(url_for("config.index", tab="agents"))


@bp.route("/agents/<int:agent_id>/command", methods=["POST"])
@admin_required
def agent_command(agent_id: int):
    agent = db.get_or_404(RemoteAgent, agent_id)
    cmd = request.form.get("command", "").strip()
    if cmd not in ("rescan", "update", "uninstall"):
        flash("Unknown command.", "danger")
        return redirect(url_for("config.index", tab="agents"))
    agent.pending_command = cmd
    db.session.commit()
    audit(current_user.username, "agent_command", agent.name,
          f"Queued command '{cmd}' for agent '{agent.name}'")
    flash(f"Command '{cmd}' queued — agent will pick it up on next check-in.", "success")
    return redirect(url_for("config.index", tab="agents"))


@bp.route("/agents/<int:agent_id>/set-interval", methods=["POST"])
@admin_required
def agent_set_interval(agent_id: int):
    import json
    agent = db.get_or_404(RemoteAgent, agent_id)
    try:
        minutes = max(1, int(request.form.get("scan_interval_minutes", 60)))
    except (ValueError, TypeError):
        flash("Invalid interval value.", "danger")
        return redirect(url_for("config.index", tab="agents"))
    agent.scan_interval_minutes = minutes
    agent.pending_command = "config"
    agent.pending_command_config = json.dumps({"scan_interval_minutes": minutes})
    db.session.commit()
    audit(current_user.username, "agent_interval", agent.name,
          f"Set scan interval to {minutes} min for agent '{agent.name}'")
    flash(f"Scan interval set to {minutes} min — pushed to agent on next check-in.", "success")
    return redirect(url_for("config.index", tab="agents"))


@bp.route("/agents/<int:agent_id>/set-subnet", methods=["POST"])
@admin_required
def agent_set_subnet(agent_id: int):
    import json
    agent = db.get_or_404(RemoteAgent, agent_id)
    subnet = request.form.get("subnet", "").strip()
    agent.subnet = subnet or None
    agent.pending_command = "config"
    agent.pending_command_config = json.dumps(
        {"subnets": [subnet] if subnet else []}
    )
    db.session.commit()
    audit(current_user.username, "agent_subnet", agent.name,
          f"Set subnet to '{subnet or '(auto)'}' for agent '{agent.name}'")
    if subnet:
        flash(f"Subnet set to '{subnet}' — pushed to agent on next check-in.", "success")
    else:
        flash("Subnet cleared — agent will auto-detect on next scan.", "success")
    return redirect(url_for("config.index", tab="agents"))


@bp.route("/agents/update-all", methods=["POST"])
@admin_required
def agent_update_all():
    from app.utils.version import get_current_version
    current_ver = get_current_version()
    agents = db.session.query(RemoteAgent).filter(
        RemoteAgent.agent_version != current_ver,
        RemoteAgent.agent_version.isnot(None),
    ).all()
    count = 0
    for agent in agents:
        if agent.pending_command is None:
            agent.pending_command = "update"
            count += 1
    db.session.commit()
    audit(current_user.username, "agent_update_all", "all",
          f"Queued update for {count} outdated agent(s)")
    flash(f"Update queued for {count} outdated agent(s).", "success" if count else "info")
    return redirect(url_for("config.index", tab="agents"))


@bp.route("/agents/<int:agent_id>/set-location", methods=["POST"])
@admin_required
def agent_set_location(agent_id: int):
    agent = db.get_or_404(RemoteAgent, agent_id)
    loc_id_raw = request.form.get("location_id") or None
    loc_id = int(loc_id_raw) if loc_id_raw else None
    agent.location_id = loc_id
    # Back-fill location onto all printers under this agent
    if loc_id:
        db.session.query(Printer).filter_by(agent_id=agent_id).update(
            {"location_id": loc_id}
        )
    db.session.commit()
    loc = db.session.get(Location, loc_id) if loc_id else None
    loc_name = loc.name if loc else "(none)"
    audit(current_user.username, "agent_location", agent.name,
          f"Set location to '{loc_name}' for agent '{agent.name}'")
    flash(f"Location updated to '{loc_name}'.", "success")
    return redirect(url_for("config.index", tab="agents"))


@bp.route("/agents/<int:agent_id>/regenerate-key", methods=["POST"])
@admin_required
def agent_regenerate_key(agent_id: int):
    import hashlib
    import secrets
    from flask import session as flask_session

    agent = db.get_or_404(RemoteAgent, agent_id)
    plaintext_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    agent.api_key_hash = key_hash
    db.session.commit()

    public_url = _get_setting("public_url", "https://your-dashboard-url.com")
    subnet_hint = agent.subnet or "192.168.1.0/24"
    loc_name = agent.location.name if agent.location else ""

    windows_cmd = (
        f'$env:AGENT_URL="{public_url}"; $env:AGENT_KEY="{plaintext_key}"; '
        f'$env:AGENT_SUBNET="{subnet_hint}"; $env:AGENT_LOCATION="{loc_name}"; '
        f'irm -Headers @{{"X-Agent-Key"=$env:AGENT_KEY}} "$env:AGENT_URL/api/agent/download/install_windows.ps1" | iex'
    )
    pi_cmd = (
        f'AGENT_URL="{public_url}" AGENT_KEY="{plaintext_key}" '
        f'AGENT_SUBNET="{subnet_hint}" AGENT_LOCATION="{loc_name}" '
        f'bash <(curl -sSL -H "X-Agent-Key: {plaintext_key}" "{public_url}/api/agent/download/install_pi.sh")'
    )

    flask_session["new_agent_info"] = {
        "agent_name": agent.name,
        "plaintext_key": plaintext_key,
        "windows_cmd": windows_cmd,
        "pi_cmd": pi_cmd,
        "agent_id": agent_id,
        "regen": True,
    }

    audit(current_user.username, "agent_regen_key", agent.name,
          f"Regenerated API key for agent '{agent.name}'")
    return redirect(url_for("config.index", tab="agents"))


@bp.route("/agents/<int:agent_id>/delete", methods=["POST"])
@admin_required
def delete_agent(agent_id: int):
    agent = db.get_or_404(RemoteAgent, agent_id)
    force = request.form.get("force") == "1"
    agent_name = agent.name

    if force or agent.status == "stale":
        # Hard-delete: agent is offline, remove the DB row and orphan remote printers
        db.session.query(Printer).filter_by(agent_id=agent_id).update(
            {"agent_id": None, "is_active": False}
        )
        db.session.delete(agent)
        db.session.commit()
        audit(current_user.username, "agent_force_delete", agent_name,
              f"Force-deleted offline agent '{agent_name}'")
        flash(f"Agent '{agent_name}' deleted. Its printers have been deactivated.", "success")
    else:
        # Queue uninstall command — agent deletes itself and row is removed on ACK
        agent.pending_command = "uninstall"
        db.session.commit()
        audit(current_user.username, "agent_delete", agent_name,
              f"Queued uninstall for agent '{agent_name}'")
        flash(
            f"Uninstall command queued for '{agent_name}'. "
            f"The agent will remove itself on next check-in and the row will disappear.",
            "info",
        )
    return redirect(url_for("config.index", tab="agents"))
