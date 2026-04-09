from __future__ import annotations

from flask import Blueprint, render_template

from app.core.database import db
from app.models import Printer

bp = Blueprint("history", __name__, url_prefix="/history")


@bp.route("/<int:printer_id>")
def printer_history(printer_id: int):
    printer = db.get_or_404(Printer, printer_id)
    return render_template("history/printer.html", printer=printer)
