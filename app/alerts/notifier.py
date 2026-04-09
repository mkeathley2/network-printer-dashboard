"""
SMTP email alert sender.
Sends one-shot HTML + plain-text email notifications via STARTTLS (port 587).
Errors are logged but never raised so that the polling loop is not interrupted.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.core.config import config
from app.snmp.normalizer import SupplyData

logger = logging.getLogger(__name__)

# Human-readable event descriptions
EVENT_LABELS = {
    "toner_warning":   "Toner Low Warning",
    "toner_critical":  "Toner Critically Low",
    "toner_replaced":  "Toner Replaced",
    "drum_warning":    "Drum Life Low Warning",
    "drum_critical":   "Drum Life Critically Low",
    "drum_replaced":   "Drum Unit Replaced",
    "printer_offline": "Printer Offline",
    "printer_online":  "Printer Back Online",
    "discovery_new":   "New Printer Discovered",
}


def send_alert_email(
    event_type: str,
    printer,
    supply: Optional[SupplyData],
    level_pct: Optional[int],
) -> None:
    """
    Send an alert email for the given event.
    Silently returns (logs error) if SMTP is not configured or sending fails.
    """
    cfg = config.smtp
    if not cfg.enabled:
        logger.debug("SMTP not configured; skipping email for %s", event_type)
        return

    recipients = config.alerts.alert_to
    if not recipients:
        logger.debug("No alert_to recipients configured; skipping email.")
        return

    subject, body_text, body_html = _build_message(event_type, printer, supply, level_pct)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr or cfg.user
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg.user, cfg.password)
            server.sendmail(msg["From"], recipients, msg.as_string())
        logger.info("Alert email sent: %s → %s for printer %s", event_type, recipients, printer.ip_address)
    except Exception as e:
        logger.error("Failed to send alert email for %s: %s", event_type, e)


def _build_message(event_type, printer, supply, level_pct):
    label = EVENT_LABELS.get(event_type, event_type)
    printer_name = printer.effective_name
    printer_ip = printer.ip_address
    printer_model = printer.model or "Unknown model"

    supply_info = ""
    supply_info_html = ""
    if supply:
        color_label = (supply.supply_color or "").title()
        supply_desc = supply.description or supply.supply_type or "Supply"
        pct_str = f"{level_pct}%" if level_pct is not None else "Unknown"
        supply_info = f"Supply: {color_label} {supply_desc} — Level: {pct_str}"
        supply_info_html = f"<tr><td><strong>Supply</strong></td><td>{color_label} {supply_desc}</td></tr>"
        if level_pct is not None:
            supply_info_html += f"<tr><td><strong>Level Remaining</strong></td><td>{pct_str}</td></tr>"

    subject = f"[Printer Alert] {label} — {printer_name}"

    body_text = f"""\
Printer Alert: {label}

Printer Name : {printer_name}
IP Address   : {printer_ip}
Model        : {printer_model}
{supply_info}

This is an automated message from the Network Printer Dashboard.
"""

    body_html = f"""\
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
  <h2 style="color: #c0392b;">Printer Alert: {label}</h2>
  <table cellpadding="6" cellspacing="0" border="0" style="border-collapse:collapse;">
    <tr><td><strong>Printer Name</strong></td><td>{printer_name}</td></tr>
    <tr><td><strong>IP Address</strong></td><td>{printer_ip}</td></tr>
    <tr><td><strong>Model</strong></td><td>{printer_model}</td></tr>
    {supply_info_html}
  </table>
  <hr/>
  <p style="font-size:12px;color:#999;">
    Automated alert from the <strong>Network Printer Dashboard</strong>.
    To adjust alert thresholds, edit <code>config.yaml</code>.
  </p>
</body>
</html>
"""

    return subject, body_text, body_html
