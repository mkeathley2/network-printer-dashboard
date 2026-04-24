"""
Help / User Manual — single-page in-app documentation.
Accessible to all logged-in users; admin-only sections are hidden for viewers
via Jinja {% if current_user.is_admin %} inside the template.
"""
from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required

bp = Blueprint("help", __name__, url_prefix="/help")


@bp.route("/")
@login_required
def index():
    return render_template("help/index.html")
