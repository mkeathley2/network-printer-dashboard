"""Admin-only configuration routes: SMTP, Users, Groups."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.security import generate_password_hash

from app.core.database import db
from app.models import Printer, SiteSetting, User
from app.models.printer import PrinterGroup
from app.web.routes.auth import admin_required

bp = Blueprint("config", __name__, url_prefix="/config")

# Setting keys stored in SiteSetting table
SMTP_KEYS = ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "alert_to", "helpdesk_email"]

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
    groups = (
        db.session.query(PrinterGroup)
        .order_by(PrinterGroup.name)
        .all()
    )
    # Attach printer count to each group
    group_counts = {}
    for g in groups:
        group_counts[g.id] = db.session.query(Printer).filter_by(group_id=g.id, is_active=True).count()

    warn_pct = _get_setting("supply_warn_pct", str(THRESHOLD_WARN_DEFAULT))
    crit_pct = _get_setting("supply_crit_pct", str(THRESHOLD_CRIT_DEFAULT))

    tab = request.args.get("tab", "smtp")
    return render_template(
        "config/index.html",
        smtp=smtp,
        users=users,
        groups=groups,
        group_counts=group_counts,
        active_tab=tab,
        warn_pct=warn_pct,
        crit_pct=crit_pct,
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
    flash("Threshold settings saved.", "success")
    return redirect(url_for("config.index", tab="thresholds"))


# ---------------------------------------------------------------------------
# SMTP settings
# ---------------------------------------------------------------------------
@bp.route("/smtp", methods=["POST"])
@admin_required
def save_smtp():
    for key in ["smtp_host", "smtp_port", "smtp_user", "smtp_from", "alert_to", "helpdesk_email"]:
        _set_setting(key, request.form.get(key, "").strip())
    # Only update password if a new one was supplied
    new_pw = request.form.get("smtp_password", "").strip()
    if new_pw:
        _set_setting("smtp_password", new_pw)
    db.session.commit()
    flash("SMTP settings saved.", "success")
    return redirect(url_for("config.index", tab="smtp"))


@bp.route("/smtp/test", methods=["POST"])
@admin_required
def test_smtp():
    from app.alerts.notifier import send_test_email
    ok, msg = send_test_email()
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
    flash(f"User '{username}' created.", "success")
    return redirect(url_for("config.index", tab="users"))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("config.index", tab="users"))
    db.session.delete(user)
    db.session.commit()
    flash(f"User '{user.username}' deleted.", "success")
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
    flash(f"Role for '{user.username}' updated to {role}.", "success")
    return redirect(url_for("config.index", tab="users"))


# ---------------------------------------------------------------------------
# Group management
# ---------------------------------------------------------------------------
@bp.route("/groups/add", methods=["POST"])
@admin_required
def add_group():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip() or None
    if not name:
        flash("Group name is required.", "danger")
        return redirect(url_for("config.index", tab="groups"))
    if db.session.query(PrinterGroup).filter_by(name=name).first():
        flash(f"Group '{name}' already exists.", "danger")
        return redirect(url_for("config.index", tab="groups"))
    db.session.add(PrinterGroup(name=name, description=description))
    db.session.commit()
    flash(f"Group '{name}' created.", "success")
    return redirect(url_for("config.index", tab="groups"))


@bp.route("/groups/<int:group_id>/delete", methods=["POST"])
@admin_required
def delete_group(group_id: int):
    group = db.get_or_404(PrinterGroup, group_id)
    # Unassign printers (FK is SET NULL on delete, but let's be explicit)
    db.session.query(Printer).filter_by(group_id=group_id).update({"group_id": None})
    db.session.delete(group)
    db.session.commit()
    flash(f"Group '{group.name}' deleted.", "success")
    return redirect(url_for("config.index", tab="groups"))
