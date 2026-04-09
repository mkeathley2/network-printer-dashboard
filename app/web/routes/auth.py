"""Authentication routes and access-control decorators."""
from __future__ import annotations

from datetime import datetime
from functools import wraps

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from app.core.database import db
from app.models import User

bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------------------
# Access-control decorator
# ---------------------------------------------------------------------------
def admin_required(f):
    """Decorator: must be logged in AND have role='admin'. Returns 403 otherwise."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        user = db.session.query(User).filter_by(username=username, is_active=True).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=remember)
            user.last_login_at = datetime.utcnow()
            db.session.commit()
            next_page = request.args.get("next")
            return redirect(next_page or url_for("dashboard.index"))

        flash("Invalid username or password.", "danger")

    return render_template("auth/login.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Profile (change own password)
# ---------------------------------------------------------------------------
@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not check_password_hash(current_user.password_hash, current_password):
            flash("Current password is incorrect.", "danger")
        elif len(new_password) < 4:
            flash("New password must be at least 4 characters.", "danger")
        elif new_password != confirm:
            flash("New passwords do not match.", "danger")
        else:
            current_user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("Password updated successfully.", "success")
            return redirect(url_for("auth.profile"))

    return render_template("auth/profile.html")


# ---------------------------------------------------------------------------
# 403 handler
# ---------------------------------------------------------------------------
def register_error_handlers(app):
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403
