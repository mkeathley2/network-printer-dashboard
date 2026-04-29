"""
Application entry point.
Creates the Flask app, wires up APScheduler, then starts the server.
"""
from __future__ import annotations

import logging
import os

from app.core.extensions import scheduler
from app.web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = create_app()


def _scheduled_poll() -> None:
    """Wrapper so the scheduler job runs inside the Flask app context."""
    with app.app_context():
        try:
            from app.core.config import config
            from app.core.database import get_db
            from app.scanner.poller import poll_all_printers

            with get_db() as db_session:
                poll_all_printers(db_session)
        except Exception:
            logger.exception("Error during scheduled poll")


# Wire up the poll job — interval comes from DB setting if present, else config.yaml default
from app.core.config import config  # noqa: E402 (after app creation)

with app.app_context():
    from app.core.database import db
    from app.models import SiteSetting
    row = db.session.get(SiteSetting, "poll_interval_minutes")
    _interval = int(row.value) if (row and row.value) else config.polling.interval_minutes

scheduler.add_job(
    _scheduled_poll,
    trigger="interval",
    minutes=_interval,
    id="poll_job",
    replace_existing=True,
)


def _check_stale_agents() -> None:
    """
    Every 5 minutes: flip agents to 'stale' if they haven't checked in within
    2× their scan_interval_minutes, and send a one-time alert email.
    Also clears the 'stale' status if an agent has already come back
    (which is handled on checkin, but this re-checks in case of partial updates).
    """
    with app.app_context():
        try:
            from app.core.database import get_db
            from app.models.remote_agent import RemoteAgent

            with get_db() as db_session:
                agents = db_session.query(RemoteAgent).filter(
                    RemoteAgent.status.in_(["active", "stale"])
                ).all()

                for agent in agents:
                    if agent.last_checkin_at is None:
                        continue
                    stale_minutes = agent.scan_interval_minutes * 2
                    elapsed_minutes = (
                        datetime.utcnow() - agent.last_checkin_at
                    ).total_seconds() / 60

                    if elapsed_minutes >= stale_minutes:
                        agent.status = "stale"
                        if not agent.stale_alert_sent:
                            _send_agent_stale_alert(agent)
                            agent.stale_alert_sent = True
        except Exception:
            logger.exception("Error during stale-agent check")


def _send_agent_stale_alert(agent) -> None:
    """Send a one-time email when a remote agent goes stale."""
    try:
        from app.alerts.notifier import get_smtp_settings, _send_email
        smtp = get_smtp_settings()
        if not smtp["enabled"] or not smtp["alert_to"]:
            return

        loc_name = agent.location.name if agent.location else "No location"
        last_seen = agent.last_checkin_at.strftime("%Y-%m-%d %H:%M UTC") if agent.last_checkin_at else "never"

        subject = f"[Printer Alert] Remote Agent Offline — {agent.name}"
        body_text = (
            f"Remote Agent Offline\n\n"
            f"Agent:      {agent.name}\n"
            f"Location:   {loc_name}\n"
            f"Last Seen:  {last_seen}\n"
            f"Subnet:     {agent.subnet or 'unknown'}\n\n"
            f"The agent has not checked in for more than {agent.scan_interval_minutes * 2} minutes.\n"
            f"Printer data from this site is frozen until the agent reconnects.\n\n"
            f"This is an automated message from the Network Printer Dashboard."
        )
        body_html = f"""\
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;">
  <h2 style="color:#c0392b;">Remote Agent Offline</h2>
  <table cellpadding="6" cellspacing="0" border="0" style="border-collapse:collapse;">
    <tr><td><strong>Agent</strong></td><td>{agent.name}</td></tr>
    <tr><td><strong>Location</strong></td><td>{loc_name}</td></tr>
    <tr><td><strong>Last Seen</strong></td><td>{last_seen}</td></tr>
    <tr><td><strong>Subnet</strong></td><td>{agent.subnet or 'unknown'}</td></tr>
  </table>
  <p>The agent has not checked in for more than {agent.scan_interval_minutes * 2} minutes.<br>
  Printer data from this site is <strong>frozen</strong> until the agent reconnects.</p>
  <hr/>
  <p style="font-size:12px;color:#999;">Automated alert from the <strong>Network Printer Dashboard</strong>.</p>
</body></html>"""

        _send_email(subject, body_text, body_html, smtp["alert_to"])
        logger.info("Stale-agent alert sent for '%s'", agent.name)
    except Exception:
        logger.exception("Failed to send stale-agent alert for '%s'", agent.name)


# Import datetime for the stale check and predictive alerts
from datetime import datetime, timedelta  # noqa: E402

scheduler.add_job(
    _check_stale_agents,
    trigger="interval",
    minutes=5,
    id="stale_agent_check",
    replace_existing=True,
)


def _check_predictive_toner() -> None:
    """
    Every hour: for each active printer supply, fit a linear regression to recent
    SupplySnapshot readings. If the supply is predicted to run out within the configured
    threshold, auto-create a helpdesk ticket (once per supply lifecycle).
    """
    with app.app_context():
        try:
            from app.core.database import get_db
            from app.models import SiteSetting

            # Check if feature is enabled
            enabled_row = db.session.get(SiteSetting, "predictive_toner_enabled")
            if not (enabled_row and enabled_row.value == "1"):
                return

            threshold_row = db.session.get(SiteSetting, "predictive_toner_days")
            min_pts_row = db.session.get(SiteSetting, "predictive_toner_min_points")
            threshold_days = int(threshold_row.value) if threshold_row and threshold_row.value else 7
            min_points = int(min_pts_row.value) if min_pts_row and min_pts_row.value else 5

            from app.models import Printer, SupplySnapshot
            from app.models.alert import AlertState
            from app.core.database import db
            from app.utils.depletion import compute_supply_depletion

            printers = db.session.query(Printer).filter_by(is_active=True).all()
            for printer in printers:
                # Get distinct supply indexes for this printer
                indexes = [
                    row[0] for row in
                    db.session.query(SupplySnapshot.supply_index).filter(
                        SupplySnapshot.printer_id == printer.id,
                        SupplySnapshot.supply_type == "tonerCartridge",
                        SupplySnapshot.level_pct.isnot(None),
                    ).distinct().all()
                ]

                for idx in indexes:
                    # Replacement-aware depletion estimate — only fits to data
                    # since the most recent toner_replaced event for this slot.
                    d = compute_supply_depletion(printer.id, idx, db.session, window_days=90)
                    if not d:
                        continue
                    if d["data_points"] < min_points:
                        continue
                    if d["days_remaining"] is None:
                        continue  # Not depleting
                    if d["days_remaining"] > threshold_days:
                        continue

                    # Check dedup via AlertState.predictive_alert_sent
                    state = db.session.query(AlertState).filter_by(
                        printer_id=printer.id, supply_index=idx
                    ).first()
                    if state and state.predictive_alert_sent:
                        continue

                    # Need the latest reading for color/description in the ticket
                    latest_reading = (
                        db.session.query(SupplySnapshot)
                        .filter_by(printer_id=printer.id, supply_index=idx)
                        .order_by(SupplySnapshot.polled_at.desc())
                        .first()
                    )

                    # Fire the ticket
                    _send_predictive_ticket(
                        printer, latest_reading,
                        d["days_remaining"], abs(d["slope_pct_per_day"]),
                        d["data_points"],
                    )

                    # Mark sent
                    if not state:
                        state = AlertState(
                            printer_id=printer.id,
                            supply_index=idx,
                            alert_level="none",
                        )
                        db.session.add(state)
                    state.predictive_alert_sent = True
                    db.session.commit()

        except Exception:
            logger.exception("Error during predictive toner check")


def _send_predictive_ticket(printer, latest_reading, days_remaining: float,
                             pct_per_day: float, data_points: int) -> None:
    """Send a helpdesk ticket for a predicted toner runout."""
    try:
        from app.alerts.notifier import send_helpdesk_ticket

        color = (latest_reading.supply_color or "unknown").title()
        desc = latest_reading.supply_description or f"{color} Toner"
        current_pct = latest_reading.level_pct
        loc_name = printer.location.name if getattr(printer, "location", None) else "Unknown"
        predicted_date = (datetime.utcnow() + timedelta(days=days_remaining)).strftime("%Y-%m-%d")

        note = (
            f"PREDICTIVE TONER ALERT (auto-generated)\n\n"
            f"Supply:           {desc}\n"
            f"Current Level:    {current_pct}%\n"
            f"Consumption Rate: {pct_per_day:.2f}% per day\n"
            f"Estimated Days Remaining: {days_remaining:.1f} days\n"
            f"Predicted Empty:  {predicted_date}\n"
            f"Based on {data_points} readings.\n\n"
            f"Please order or replace this supply soon."
        )

        ok, msg = send_helpdesk_ticket(printer, [], note, "system/predictive")
        if ok:
            logger.info("Predictive ticket sent for '%s' supply %s (%s, %.1f days)",
                        printer.effective_name, desc, color, days_remaining)
        else:
            logger.warning("Predictive ticket failed for '%s': %s", printer.effective_name, msg)
    except Exception:
        logger.exception("Failed to send predictive ticket for '%s'", printer.effective_name)


scheduler.add_job(
    _check_predictive_toner,
    trigger="interval",
    hours=1,
    id="predictive_toner_check",
    replace_existing=True,
)

scheduler.start()
logger.info("Scheduler started. Poll interval: %d minutes", _interval)

if __name__ == "__main__":
    # Development server only; production uses gunicorn
    app.run(
        host="0.0.0.0",
        port=config.app.port,
        debug=config.app.debug,
        use_reloader=False,  # CRITICAL: reloader spawns second process → double scheduler
    )
