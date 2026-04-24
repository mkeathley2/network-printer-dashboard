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
            login_user(user, remember=False if user.must_change_password else remember)
            user.last_login_at = datetime.utcnow()
            db.session.commit()
            if user.must_change_password:
                return redirect(url_for("auth.change_password"))
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
# Forced password change (temp password flow)
# ---------------------------------------------------------------------------
@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """
    Required step when must_change_password is True.
    User sets a new password without needing to know the old one
    (they already authenticated with the temp password to get here).
    """
    if not current_user.must_change_password:
        # Nothing to do — send them on their way
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if len(new_password) < 8:
            flash("Password must be at least 8 characters.", "danger")
        elif new_password != confirm:
            flash("Passwords do not match.", "danger")
        else:
            current_user.password_hash = generate_password_hash(new_password)
            current_user.must_change_password = False
            db.session.commit()
            flash("Password set successfully. Welcome!", "success")
            return redirect(url_for("dashboard.index"))

    return render_template("auth/change_password.html")


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
# 403 handler + before_request guard
# ---------------------------------------------------------------------------
def register_error_handlers(app):
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.before_request
    def enforce_password_change():
        """
        If the logged-in user has must_change_password set, redirect every
        request to the change-password page (except the page itself, logout,
        and static assets).
        """
        if not current_user.is_authenticated:
            return
        if not current_user.must_change_password:
            return
        allowed_endpoints = {"auth.change_password", "auth.logout", "static"}
        if request.endpoint in allowed_endpoints:
            return
        return redirect(url_for("auth.change_password"))
