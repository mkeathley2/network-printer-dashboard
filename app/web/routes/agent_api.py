"""
Remote Agent API — endpoints consumed by printer_agent.py deployed at remote sites.

Authentication: All agent routes verify X-Agent-Key header against a SHA-256 hash
stored in the remote_agents table.  Admin-session auth is accepted on download routes
so admins can also fetch files from the browser.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from functools import wraps

from flask import Blueprint, abort, g, jsonify, request, send_file

from app.core.database import db
from app.models import Location, Printer, TelemetrySnapshot, SupplySnapshot
from app.models.remote_agent import RemoteAgent
from app.snmp.normalizer import PrinterData, SupplyData

logger = logging.getLogger(__name__)

bp = Blueprint("agent_api", __name__, url_prefix="/api/agent")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _require_agent(f):
    """Decorator: validate X-Agent-Key, inject g.agent."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Agent-Key", "").strip()
        if not key:
            return jsonify({"error": "Missing X-Agent-Key header"}), 401
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        agent = db.session.query(RemoteAgent).filter_by(api_key_hash=key_hash).first()
        if not agent:
            return jsonify({"error": "Invalid API key"}), 401
        g.agent = agent
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# POST /api/agent/checkin
# ---------------------------------------------------------------------------

@bp.route("/checkin", methods=["POST"])
@_require_agent
def checkin():
    agent: RemoteAgent = g.agent
    data = request.get_json(force=True, silent=True) or {}

    # --- Update agent metadata ---
    agent.last_checkin_at = datetime.utcnow()
    agent.last_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if data.get("agent_version"):
        agent.agent_version = data["agent_version"][:32]
        # Auto-queue update if agent is outdated and no other command is pending
        try:
            from app.utils.version import get_current_version
            current_ver = get_current_version()
            if (current_ver != "unknown"
                    and agent.agent_version != current_ver
                    and agent.pending_command is None):
                agent.pending_command = "update"
                logger.info(
                    "Agent '%s' is %s, dashboard is %s — queuing auto-update",
                    agent.name, agent.agent_version, current_ver,
                )
        except Exception:
            pass
    if data.get("subnet"):
        agent.subnet = data["subnet"][:64]
    agent.status = "active"
    agent.stale_alert_sent = False

    # Store scan errors (keep last 50)
    errors = data.get("errors") or []
    if isinstance(errors, list) and errors:
        agent.last_errors = json.dumps(errors[-50:])

    # --- Auto-assign location from location_name if agent has none ---
    location_name = (data.get("location_name") or "").strip()
    if location_name and not agent.location_id:
        loc = (
            db.session.query(Location)
            .filter(Location.name.ilike(location_name))
            .first()
        )
        if not loc:
            loc = Location(name=location_name)
            db.session.add(loc)
            db.session.flush()
        agent.location_id = loc.id
        # Back-fill onto printers that arrived before location was known
        db.session.query(Printer).filter_by(
            agent_id=agent.id, location_id=None
        ).update({"location_id": loc.id})

    # --- Process printer data ---
    incoming_ips: set[str] = set()
    for p_data in (data.get("printers") or []):
        ip = (p_data.get("ip") or "").strip()
        if not ip:
            continue
        incoming_ips.add(ip)
        try:
            _process_printer(agent, ip, p_data)
        except Exception:
            logger.exception("Failed to process printer %s for agent %s", ip, agent.name)

    # --- Handle printers missing from this checkin ---
    active_printers = (
        db.session.query(Printer)
        .filter_by(agent_id=agent.id, is_active=True)
        .all()
    )
    for printer in active_printers:
        if printer.ip_address not in incoming_ips:
            printer.consecutive_misses = (printer.consecutive_misses or 0) + 1
            if printer.consecutive_misses >= 3 and printer.is_online:
                _mark_printer_offline(printer)

    # --- Pop pending command ---
    cmd = agent.pending_command
    cmd_config = None
    if cmd == "config" and agent.pending_command_config:
        try:
            cmd_config = json.loads(agent.pending_command_config)
        except Exception:
            pass
    agent.pending_command = None
    agent.pending_command_config = None

    # If we just told the agent to uninstall, delete the row now so it's gone on the next page load.
    # The agent will stop itself after receiving this response.
    if cmd == "uninstall":
        db.session.query(Printer).filter_by(agent_id=agent.id).update(
            {"agent_id": None, "is_active": False}
        )
        db.session.delete(agent)
        db.session.commit()
        return jsonify({"status": "ok", "command": "uninstall"})

    db.session.commit()

    # --- Build response ---
    resp: dict = {"status": "ok", "command": cmd}
    try:
        from app.utils.version import get_current_version
        resp["latest_version"] = get_current_version()
    except Exception:
        pass
    if cmd == "update":
        # Keep "version" key for backward compatibility with older agents
        resp["version"] = resp.get("latest_version", "unknown")
    elif cmd == "config" and cmd_config:
        resp["config"] = cmd_config

    return jsonify(resp)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _process_printer(agent: RemoteAgent, ip: str, p_data: dict) -> None:
    """Upsert a printer record from checkin data, write telemetry, run alerts."""
    printer = (
        db.session.query(Printer)
        .filter_by(ip_address=ip, agent_id=agent.id)
        .first()
    )

    if not printer:
        printer = Printer(
            ip_address=ip,
            agent_id=agent.id,
            location_id=agent.location_id,
            is_active=True,
            is_online=False,
            snmp_community="public",
        )
        db.session.add(printer)
        db.session.flush()

    # --- Update fields ---
    vendor_raw = (p_data.get("vendor") or "generic").lower()
    valid_vendors = ("hp", "brother", "canon", "kyocera", "ricoh", "generic")
    printer.vendor = vendor_raw if vendor_raw in valid_vendors else "generic"
    if p_data.get("model"):
        printer.model = str(p_data["model"])[:255]
    if p_data.get("serial"):
        printer.serial_number = str(p_data["serial"])[:128]
    if p_data.get("hostname"):
        printer.hostname = str(p_data["hostname"])[:255]

    printer.is_online = bool(p_data.get("is_online", True))
    if printer.is_online:
        printer.last_seen_at = datetime.utcnow()
        printer.consecutive_failures = 0
    else:
        printer.consecutive_failures = (printer.consecutive_failures or 0) + 1
    printer.consecutive_misses = 0  # appeared in this checkin

    # --- Telemetry snapshot ---
    snapshot = TelemetrySnapshot(
        printer_id=printer.id,
        polled_at=datetime.utcnow(),
        is_online=printer.is_online,
        page_count=p_data.get("page_count"),
        uptime_seconds=p_data.get("uptime_seconds"),
    )
    db.session.add(snapshot)
    db.session.flush()

    # --- Supply snapshots + SupplyData objects for alert evaluator ---
    supply_objs: list[SupplyData] = []
    for idx, s in enumerate(p_data.get("supplies") or []):
        level_pct = s.get("level_pct")
        level_current = s.get("level_current")
        level_max = s.get("level_max")
        # Compute pct from raw values if not provided
        if level_pct is None and level_current is not None and level_max:
            try:
                from app.snmp.normalizer import compute_pct
                level_pct = compute_pct(int(level_current), int(level_max))
            except Exception:
                pass

        supply_type = (s.get("supply_type") or "tonerCartridge")
        supply_color = (s.get("color") or "unknown")
        supply_desc = (s.get("description") or "")

        ss = SupplySnapshot(
            telemetry_id=snapshot.id,
            printer_id=printer.id,
            polled_at=snapshot.polled_at,
            supply_index=idx,
            supply_type=supply_type,
            supply_color=supply_color,
            supply_description=supply_desc,
            level_current=level_current,
            level_max=level_max,
            level_pct=level_pct,
        )
        db.session.add(ss)

        supply_objs.append(SupplyData(
            supply_index=idx,
            supply_type=supply_type,
            supply_color=supply_color,
            description=supply_desc,
            level_current=level_current,
            level_max=level_max,
            level_pct=level_pct,
        ))

    # --- Alert evaluation ---
    try:
        from app.alerts.evaluator import evaluate
        pd = PrinterData(
            ip_address=ip,
            is_online=printer.is_online,
            supplies=supply_objs,
        )
        evaluate(printer, pd, db.session)
    except Exception:
        logger.exception("Alert evaluation failed for remote printer %s", ip)


def _mark_printer_offline(printer: Printer) -> None:
    """Flip printer offline and fire the offline alert via the standard evaluator."""
    printer.is_online = False
    try:
        from app.alerts.evaluator import evaluate
        from app.core.config import config
        # Set consecutive_failures to the threshold so the evaluator fires
        printer.consecutive_failures = config.alerts.offline_after_failures
        pd = PrinterData(ip_address=printer.ip_address, is_online=False)
        evaluate(printer, pd, db.session)
    except Exception:
        logger.exception("Offline evaluation failed for printer %s", printer.ip_address)


# ---------------------------------------------------------------------------
# File downloads
# ---------------------------------------------------------------------------

def _agent_dir() -> str:
    """
    Absolute path to the agent/ directory.
    In Docker:
      - __file__ = /project/app/web/routes/agent_api.py
      - project root is mounted at /project/repo/
      - agent scripts live at /project/repo/agent/
    In local dev (project root = parent of app/):
      - fall back to ../../../agent/ relative to this file
    """
    # Docker path: /project/repo/agent/
    docker_path = "/project/repo/agent"
    if os.path.isdir(docker_path):
        return docker_path
    # Local dev fallback: up 3 dirs from app/web/routes/ → project root → agent/
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "agent")
    )


def _auth_download(allow_agent_key: bool = True) -> bool:
    """
    Returns True if the request is authorized.
    Accepts either a logged-in admin session OR a valid X-Agent-Key header.
    """
    from flask_login import current_user
    if current_user.is_authenticated and getattr(current_user, "is_admin", False):
        return True
    if allow_agent_key:
        key = request.headers.get("X-Agent-Key", "").strip()
        if key:
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            agent = db.session.query(RemoteAgent).filter_by(api_key_hash=key_hash).first()
            if agent:
                return True
    return False


@bp.route("/download/agent.py")
def download_agent_script():
    if not _auth_download():
        abort(401)
    path = os.path.join(_agent_dir(), "printer_agent.py")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name="printer_agent.py",
                     mimetype="text/x-python")


@bp.route("/download/install_windows.ps1")
def download_install_windows():
    if not _auth_download():
        abort(401)
    path = os.path.join(_agent_dir(), "install_windows.ps1")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=False, mimetype="text/plain")


@bp.route("/download/install_pi.sh")
def download_install_pi():
    if not _auth_download():
        abort(401)
    path = os.path.join(_agent_dir(), "install_pi.sh")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=False, mimetype="text/plain")
