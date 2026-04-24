"""
SMTP email alert sender.
Sends one-shot HTML + plain-text email notifications via STARTTLS (port 587).
Errors are logged but never raised so that the polling loop is not interrupted.
SMTP settings are read from SiteSetting DB table first, falling back to env/config.
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

def is_alert_enabled(event_type: str) -> bool:
    """Return True if the given alert type should send an email (default: enabled)."""
    try:
        from app.web.routes.config import _get_setting
        return _get_setting(f"alert_{event_type}", "1") == "1"
    except Exception:
        return True  # fail open — always send if we can't read the setting


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


# ---------------------------------------------------------------------------
# DB-backed SMTP settings helper
# ---------------------------------------------------------------------------
def get_smtp_settings() -> dict:
    """
    Return SMTP settings as a dict. DB SiteSetting rows take priority over
    env/config values so that changes via the UI take effect without restart.
    """
    try:
        from app.core.database import db
        from app.models import SiteSetting

        def _val(key: str, fallback: str = "") -> str:
            row = db.session.get(SiteSetting, key)
            if row and row.value:
                return row.value
            return fallback

        host = _val("smtp_host", config.smtp.host)
        port = int(_val("smtp_port", str(config.smtp.port)) or 587)
        user = _val("smtp_user", config.smtp.user)
        password = _val("smtp_password", config.smtp.password)
        from_addr = _val("smtp_from", config.smtp.from_addr)
        auth_mode = _val("smtp_auth", "starttls")   # starttls | ssl | none
        alert_to_raw = _val("alert_to", ",".join(config.alerts.alert_to))
        alert_to = [a.strip() for a in alert_to_raw.split(",") if a.strip()]

        # Enabled when host is set; auth=none doesn't need credentials
        if auth_mode == "none":
            enabled = bool(host)
        else:
            enabled = bool(host and user and password)

        return {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "from_addr": from_addr or user,
            "auth_mode": auth_mode,
            "alert_to": alert_to,
            "enabled": enabled,
        }
    except Exception as e:
        logger.warning("Could not read SMTP settings from DB, using config: %s", e)
        return {
            "host": config.smtp.host,
            "port": config.smtp.port,
            "user": config.smtp.user,
            "password": config.smtp.password,
            "from_addr": config.smtp.from_addr or config.smtp.user,
            "auth_mode": "starttls",
            "alert_to": config.alerts.alert_to,
            "enabled": config.smtp.enabled,
        }


def _send_email(subject: str, body_text: str, body_html: str, recipients: list[str]) -> tuple[bool, str]:
    """Core send logic. Returns (success, message)."""
    smtp = get_smtp_settings()
    if not smtp["enabled"]:
        return False, "SMTP is not configured."
    if not recipients:
        return False, "No recipients specified."

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp["from_addr"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        auth_mode = smtp.get("auth_mode", "starttls")
        if auth_mode == "ssl":
            # SMTP_SSL — used for port 465
            with smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=15) as server:
                server.login(smtp["user"], smtp["password"])
                server.sendmail(smtp["from_addr"], recipients, msg.as_string())
        elif auth_mode == "none":
            # Unauthenticated — local relay, no TLS
            with smtplib.SMTP(smtp["host"], smtp["port"], timeout=15) as server:
                server.ehlo()
                server.sendmail(smtp["from_addr"], recipients, msg.as_string())
        else:
            # Default: STARTTLS (port 587)
            with smtplib.SMTP(smtp["host"], smtp["port"], timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(smtp["user"], smtp["password"])
                server.sendmail(smtp["from_addr"], recipients, msg.as_string())
        return True, f"Email sent to {', '.join(recipients)}."
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False, f"Failed to send email: {e}"


# ---------------------------------------------------------------------------
# User account emails (welcome / password reset)
# ---------------------------------------------------------------------------

def send_welcome_email(
    user_email: str,
    username: str,
    temp_password: str,
    dashboard_url: str,
) -> tuple[bool, str]:
    """Send a welcome email to a newly created user with their temporary password."""
    subject = "Your Network Printer Dashboard Account"
    body_text = (
        f"Welcome, {username}!\n\n"
        f"An account has been created for you on the Network Printer Dashboard.\n\n"
        f"Login URL:          {dashboard_url}/login\n"
        f"Username:           {username}\n"
        f"Temporary Password: {temp_password}\n\n"
        f"You will be required to set a new password the first time you log in.\n\n"
        f"This is an automated message from the Network Printer Dashboard."
    )
    body_html = f"""\
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;max-width:520px;margin:0 auto;">
  <div style="text-align:center;padding:24px 0 8px;">
    <span style="font-size:2.5rem;">🖨️</span>
    <h2 style="margin:8px 0 4px;">Network Printer Dashboard</h2>
    <p style="color:#666;margin:0;">Your account is ready</p>
  </div>
  <div style="background:#f8f9fa;border-radius:8px;padding:24px;margin:16px 0;">
    <p>Hi <strong>{username}</strong>,</p>
    <p>An account has been created for you. Use the credentials below to sign in:</p>
    <table cellpadding="8" cellspacing="0" style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="color:#666;width:40%;">Login URL</td>
        <td><a href="{dashboard_url}/login">{dashboard_url}/login</a></td>
      </tr>
      <tr style="background:#fff;border-radius:4px;">
        <td style="color:#666;">Username</td>
        <td><strong>{username}</strong></td>
      </tr>
      <tr>
        <td style="color:#666;">Temporary Password</td>
        <td><code style="background:#e9ecef;padding:2px 6px;border-radius:4px;font-size:1.1em;">{temp_password}</code></td>
      </tr>
    </table>
    <p style="margin-top:16px;color:#c0392b;"><strong>⚠️ You will be asked to set a new password on your first login.</strong></p>
  </div>
  <p style="font-size:12px;color:#999;text-align:center;">
    Automated message from the <strong>Network Printer Dashboard</strong>.
  </p>
</body></html>"""
    return _send_email(subject, body_text, body_html, [user_email])


def send_password_reset_email(
    user_email: str,
    username: str,
    temp_password: str,
    dashboard_url: str,
) -> tuple[bool, str]:
    """Send a password reset email with a new temporary password."""
    subject = "Network Printer Dashboard — Password Reset"
    body_text = (
        f"Hi {username},\n\n"
        f"Your password on the Network Printer Dashboard has been reset by an administrator.\n\n"
        f"Login URL:          {dashboard_url}/login\n"
        f"Username:           {username}\n"
        f"Temporary Password: {temp_password}\n\n"
        f"You will be required to set a new password the first time you log in.\n\n"
        f"If you did not expect this, please contact your administrator.\n\n"
        f"This is an automated message from the Network Printer Dashboard."
    )
    body_html = f"""\
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;max-width:520px;margin:0 auto;">
  <div style="text-align:center;padding:24px 0 8px;">
    <span style="font-size:2.5rem;">🔑</span>
    <h2 style="margin:8px 0 4px;">Password Reset</h2>
    <p style="color:#666;margin:0;">Network Printer Dashboard</p>
  </div>
  <div style="background:#f8f9fa;border-radius:8px;padding:24px;margin:16px 0;">
    <p>Hi <strong>{username}</strong>,</p>
    <p>Your password has been reset by an administrator. Use the credentials below to sign in:</p>
    <table cellpadding="8" cellspacing="0" style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="color:#666;width:40%;">Login URL</td>
        <td><a href="{dashboard_url}/login">{dashboard_url}/login</a></td>
      </tr>
      <tr style="background:#fff;border-radius:4px;">
        <td style="color:#666;">Username</td>
        <td><strong>{username}</strong></td>
      </tr>
      <tr>
        <td style="color:#666;">Temporary Password</td>
        <td><code style="background:#e9ecef;padding:2px 6px;border-radius:4px;font-size:1.1em;">{temp_password}</code></td>
      </tr>
    </table>
    <p style="margin-top:16px;color:#c0392b;"><strong>⚠️ You will be asked to set a new password on your first login.</strong></p>
  </div>
  <p style="font-size:12px;color:#999;text-align:center;">
    If you did not expect this reset, contact your administrator immediately.<br>
    Automated message from the <strong>Network Printer Dashboard</strong>.
  </p>
</body></html>"""
    return _send_email(subject, body_text, body_html, [user_email])


# ---------------------------------------------------------------------------
# Alert emails
# ---------------------------------------------------------------------------
def send_alert_email(
    event_type: str,
    printer,
    supply: Optional[SupplyData],
    level_pct: Optional[int],
) -> None:
    """Send an alert email. Silently logs on failure."""
    smtp = get_smtp_settings()
    if not smtp["enabled"]:
        logger.debug("SMTP not configured; skipping email for %s", event_type)
        return
    if not smtp["alert_to"]:
        logger.debug("No alert_to recipients; skipping email.")
        return

    subject, body_text, body_html = _build_alert_message(event_type, printer, supply, level_pct)
    ok, msg = _send_email(subject, body_text, body_html, smtp["alert_to"])
    if ok:
        logger.info("Alert email sent: %s for printer %s", event_type, printer.ip_address)
    else:
        logger.error("Alert email failed: %s", msg)


# ---------------------------------------------------------------------------
# Test email
# ---------------------------------------------------------------------------
def send_test_email() -> tuple[bool, str]:
    """Send a test email to the configured alert_to address. Returns (ok, message)."""
    smtp = get_smtp_settings()
    if not smtp["enabled"]:
        auth_mode = smtp.get("auth_mode", "starttls")
        if auth_mode == "none":
            return False, "SMTP is not configured. Enter a host address first."
        return False, "SMTP is not configured. Enter host, user, and password first."
    if not smtp["alert_to"]:
        return False, "No alert recipients configured. Set the 'Alert Recipients' field first."

    subject = "[Printer Dashboard] Test Email"
    body_text = "This is a test email from the Network Printer Dashboard. SMTP is working correctly."
    body_html = """\
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;">
  <h2 style="color:#27ae60;">Test Email</h2>
  <p>This is a test email from the <strong>Network Printer Dashboard</strong>.</p>
  <p>If you received this, your SMTP settings are working correctly.</p>
</body></html>"""

    return _send_email(subject, body_text, body_html, smtp["alert_to"])


# ---------------------------------------------------------------------------
# Helpdesk ticket email
# ---------------------------------------------------------------------------
def send_helpdesk_ticket(printer, supplies: list, note: str, sent_by: str) -> tuple[bool, str]:
    """Send a helpdesk ticket email for the given printer."""
    try:
        from app.core.database import db
        from app.models import SiteSetting
        row = db.session.get(SiteSetting, "helpdesk_email")
        helpdesk_email = row.value if (row and row.value) else ""
    except Exception:
        helpdesk_email = ""

    if not helpdesk_email:
        return False, "No helpdesk email address configured."

    printer_name = printer.effective_name
    status = "Online" if printer.is_online else "Offline"
    model = printer.model or "Unknown"
    vendor = (printer.vendor or "").upper()

    # Optional asset/assignment fields
    location_name = printer.location.name if getattr(printer, "location", None) else None
    extra_text = ""
    extra_html = ""
    for field_label, value in (
        ("Location",  location_name),
        ("Person",    getattr(printer, "assigned_person", None)),
        ("SQL #",     getattr(printer, "sql_number", None)),
        ("Computer",  getattr(printer, "assigned_computer", None)),
        ("Ext.",      getattr(printer, "phone_ext", None)),
    ):
        if value:
            extra_text += f"{field_label:<13}: {value}\n"
            extra_html += f"<tr><td><strong>{field_label}</strong></td><td>{value}</td></tr>"

    # Build supply rows
    supply_rows_text = ""
    supply_rows_html = ""
    for s in supplies:
        pct = f"{s.level_pct}%" if s.level_pct is not None else "Unknown"
        color = (s.supply_color or "").title()
        desc = s.supply_description or s.supply_type or "Supply"
        supply_rows_text += f"  - {color} {desc}: {pct}\n"
        bar_color = "#27ae60" if (s.level_pct or 0) > 20 else "#e74c3c"
        supply_rows_html += f"""
        <tr>
          <td>{color} {desc}</td>
          <td style="color:{bar_color};font-weight:bold;">{pct}</td>
        </tr>"""

    note_section = f"\nNote from {sent_by}:\n{note}\n" if note.strip() else ""
    note_html = f"""
    <h3>Note from {sent_by}:</h3>
    <p style="background:#f8f9fa;padding:10px;border-left:4px solid #3498db;">{note}</p>
    """ if note.strip() else ""

    subject = f"[Printer Ticket] {printer_name} ({printer.ip_address})"

    body_text = f"""\
Printer Helpdesk Ticket
Submitted by: {sent_by}

Printer Name : {printer_name}
IP Address   : {printer.ip_address}
Vendor       : {vendor}
Model        : {model}
Status       : {status}
{extra_text}
Supply Levels:
{supply_rows_text or '  No supply data available.'}
{note_section}
This ticket was created from the Network Printer Dashboard.
"""

    body_html = f"""\
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;">
  <h2 style="color:#2c3e50;">Printer Helpdesk Ticket</h2>
  <p><em>Submitted by: <strong>{sent_by}</strong></em></p>
  <table cellpadding="6" cellspacing="0" border="0" style="border-collapse:collapse;margin-bottom:16px;">
    <tr><td><strong>Printer Name</strong></td><td>{printer_name}</td></tr>
    <tr><td><strong>IP Address</strong></td><td>{printer.ip_address}</td></tr>
    <tr><td><strong>Vendor</strong></td><td>{vendor}</td></tr>
    <tr><td><strong>Model</strong></td><td>{model}</td></tr>
    <tr><td><strong>Status</strong></td><td>{"<span style='color:green'>Online</span>" if printer.is_online else "<span style='color:red'>Offline</span>"}</td></tr>
    {extra_html}
  </table>
  <h3>Supply Levels</h3>
  <table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse;border-color:#dee2e6;">
    <thead style="background:#f8f9fa;">
      <tr><th style="text-align:left;">Supply</th><th style="text-align:left;">Level</th></tr>
    </thead>
    <tbody>
      {supply_rows_html or '<tr><td colspan="2">No supply data available.</td></tr>'}
    </tbody>
  </table>
  {note_html}
  <hr/>
  <p style="font-size:12px;color:#999;">Created from the <strong>Network Printer Dashboard</strong>.</p>
</body></html>"""

    return _send_email(subject, body_text, body_html, [helpdesk_email])


# ---------------------------------------------------------------------------
# Internal message builder for alert emails
# ---------------------------------------------------------------------------
def _build_alert_message(event_type, printer, supply, level_pct):
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

    # Optional asset/assignment fields
    location_name = printer.location.name if getattr(printer, "location", None) else None
    extra_text = ""
    extra_html = ""
    for field_label, value in (
        ("Location",  location_name),
        ("Person",    getattr(printer, "assigned_person", None)),
        ("SQL #",     getattr(printer, "sql_number", None)),
        ("Computer",  getattr(printer, "assigned_computer", None)),
        ("Ext.",      getattr(printer, "phone_ext", None)),
    ):
        if value:
            extra_text += f"{field_label:<13}: {value}\n"
            extra_html += f"<tr><td><strong>{field_label}</strong></td><td>{value}</td></tr>"

    subject = f"[Printer Alert] {label} — {printer_name}"

    body_text = f"""\
Printer Alert: {label}

Printer Name : {printer_name}
IP Address   : {printer_ip}
Model        : {printer_model}
{extra_text}{supply_info}

This is an automated message from the Network Printer Dashboard.
"""

    body_html = f"""\
<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;color:#333;">
  <h2 style="color:#c0392b;">Printer Alert: {label}</h2>
  <table cellpadding="6" cellspacing="0" border="0" style="border-collapse:collapse;">
    <tr><td><strong>Printer Name</strong></td><td>{printer_name}</td></tr>
    <tr><td><strong>IP Address</strong></td><td>{printer_ip}</td></tr>
    <tr><td><strong>Model</strong></td><td>{printer_model}</td></tr>
    {extra_html}
    {supply_info_html}
  </table>
  <hr/>
  <p style="font-size:12px;color:#999;">
    Automated alert from the <strong>Network Printer Dashboard</strong>.
  </p>
</body></html>"""

    return subject, body_text, body_html
