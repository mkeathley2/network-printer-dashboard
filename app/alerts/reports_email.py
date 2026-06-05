"""
Scheduled-report emailer.

Iterates the REPORT_REGISTRY (defined in app.web.routes.reports), pulls fresh
data via each report's fetcher, builds a styled HTML email body + a CSV
attachment, and sends to the recipient configured in SiteSetting.

Three frequencies — ``"daily"``, ``"weekly"``, ``"monthly"`` — each backed by
its own APScheduler cron job in ``app/run.py``.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# Window-in-days the fetcher should look back for each frequency.
_WINDOW_FOR_FREQUENCY = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
}

_FREQUENCY_LABEL = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
}


def _get_setting(key: str, default: str = "") -> str:
    """Read a SiteSetting value. Returns default when unset."""
    try:
        from app.core.database import db
        from app.models import SiteSetting
        row = db.session.get(SiteSetting, key)
        if row and row.value is not None:
            return row.value
    except Exception:
        logger.debug("Could not read setting %s", key, exc_info=True)
    return default


def _build_csv(report: dict, rows: list) -> bytes:
    """Render the CSV attachment bytes for a report."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(report["csv_header"])
    for r in rows:
        writer.writerow(report["csv_row"](r))
    return buf.getvalue().encode("utf-8")


def _format_cell(row: dict, col_spec) -> str:
    """Each table_columns entry is (label, key_or_callable). Resolve to str."""
    if callable(col_spec):
        return str(col_spec(row))
    return str(row.get(col_spec, ""))


def _build_html(report: dict, rows: list, frequency: str, window_days: int) -> str:
    """Render the email HTML body for a single report."""
    from app.utils.dashboard_url import report_url

    title = report["title"]
    columns = report["table_columns"]
    full_url = report_url(report["path"])
    period_label = (
        f"Last {window_days} day" if window_days == 1
        else f"Last {window_days} days"
    )
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Header row
    th_html = "".join(
        f'<th style="text-align:left;padding:8px;border-bottom:2px solid #dee2e6;">{label}</th>'
        for label, _ in columns
    )

    # Data rows (cap at 50 for email — full data in CSV attachment)
    capped = rows[:50]
    if capped:
        body_rows = "".join(
            "<tr>" + "".join(
                f'<td style="padding:6px 8px;border-bottom:1px solid #f1f3f5;">'
                f'{_format_cell(row, col_spec)}</td>'
                for _, col_spec in columns
            ) + "</tr>"
            for row in capped
        )
    else:
        body_rows = (
            f'<tr><td colspan="{len(columns)}" style="padding:18px;text-align:center;color:#6c757d;">'
            f'No data in this period.</td></tr>'
        )

    more_note = ""
    if len(rows) > 50:
        more_note = (
            f'<p style="color:#6c757d;font-style:italic;margin-top:8px;">'
            f'Showing the first 50 of {len(rows)} rows. Full data in the attached CSV.</p>'
        )

    link_btn = ""
    if full_url:
        link_btn = (
            f'<p style="margin-top:18px;">'
            f'<a href="{full_url}" style="background:#0d6efd;color:#fff;padding:10px 16px;'
            f'text-decoration:none;border-radius:4px;font-weight:600;">View Full Report &amp; Charts</a>'
            f'</p>'
        )

    return f"""\
<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;color:#212529;max-width:760px;margin:0 auto;">
  <div style="background:#0d6efd;color:#fff;padding:18px 22px;border-radius:6px 6px 0 0;">
    <div style="font-size:0.85em;opacity:0.85;letter-spacing:0.05em;text-transform:uppercase;">
      {_FREQUENCY_LABEL.get(frequency, frequency.title())} Report
    </div>
    <h2 style="margin:4px 0 0;">{title}</h2>
    <div style="font-size:0.9em;opacity:0.85;margin-top:4px;">{period_label} &middot; {len(rows)} row{'' if len(rows) == 1 else 's'}</div>
  </div>
  <div style="border:1px solid #dee2e6;border-top:0;padding:18px 22px;border-radius:0 0 6px 6px;">
    <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:0.9em;">
      <thead><tr>{th_html}</tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
    {more_note}
    {link_btn}
  </div>
  <p style="font-size:12px;color:#999;text-align:center;margin-top:14px;">
    Generated {generated_at} by the <strong>Network Printer Dashboard</strong>.<br>
    Configure or unsubscribe under Config &rarr; Alert Settings.
  </p>
</body></html>"""


def _build_text(report: dict, rows: list, frequency: str, window_days: int) -> str:
    """Plain-text fallback for the email body."""
    title = report["title"]
    period_label = f"Last {window_days} day{'' if window_days == 1 else 's'}"
    lines = [
        f"{_FREQUENCY_LABEL.get(frequency, frequency.title())} Report: {title}",
        f"Period: {period_label}",
        f"Row count: {len(rows)}",
        "",
        "Top rows (full data in the attached CSV):",
        "",
    ]
    # Plain headers
    cols = report["table_columns"]
    lines.append(" | ".join(label for label, _ in cols))
    lines.append("-" * 60)
    for r in rows[:25]:
        lines.append(" | ".join(_format_cell(r, spec) for _, spec in cols))

    if len(rows) > 25:
        lines.append("")
        lines.append(f"... and {len(rows) - 25} more — see attached CSV.")

    from app.utils.dashboard_url import report_url
    full_url = report_url(report["path"])
    if full_url:
        lines += ["", f"View full report: {full_url}"]
    lines += ["", "Automated email from the Network Printer Dashboard."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bespoke per-report email layouts
# ---------------------------------------------------------------------------

_SUPPLY_COLOR_HEX = {
    "black": "#333333",
    "cyan": "#0dcaf0",
    "magenta": "#d63384",
    "yellow": "#ffc107",
}


def _build_consumption_rate_html(report: dict, rows: list, frequency: str, window_days: int) -> str:
    """
    Styled email for the Toner Consumption Rate report — mirrors the on-screen
    /reports/consumption-rate page: a "running out within 7 days" banner,
    per-color badges, an inline current-% bar, and urgent/warning row tinting.
    All inline styles for email-client compatibility.
    """
    from app.utils.dashboard_url import report_url

    title = report["title"]
    full_url = report_url(report["path"])
    period_label = f"Last {window_days} day{'' if window_days == 1 else 's'}"
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Banner: supplies predicted out within 7 days
    urgent = [r for r in rows if r.get("days_remaining") is not None and r["days_remaining"] < 7]
    banner = ""
    if urgent:
        names = ", ".join(
            f"{r['printer_name']} ({(r.get('color') or '').title()}, ~{r['days_remaining']:.1f}d)"
            for r in urgent
        )
        banner = (
            f'<div style="background:#f8d7da;color:#58151c;border:1px solid #f1aeb5;'
            f'border-radius:5px;padding:10px 14px;margin-bottom:14px;font-size:0.9em;">'
            f'<strong>⚠️ {len(urgent)} supply(ies) predicted to run out within 7 days:</strong> '
            f'{names}</div>'
        )

    # Rows
    if rows:
        body_rows = ""
        for r in rows:
            dr = r.get("days_remaining")
            row_bg = ""
            if dr is not None and dr < 7:
                row_bg = "background:#f8d7da;"
            elif dr is not None and dr < 14:
                row_bg = "background:#fff3cd;"

            color = (r.get("color") or "unknown")
            badge_bg = _SUPPLY_COLOR_HEX.get(color, "#6c757d")
            badge = (
                f'<span style="background:{badge_bg};color:#fff;padding:2px 8px;'
                f'border-radius:4px;font-size:0.8em;">{color.title()}</span>'
            )
            desc = r.get("description") or ""

            cur = r.get("current_pct") or 0
            bar_color = "#dc3545" if cur <= 10 else ("#ffc107" if cur <= 25 else "#198754")
            bar = (
                f'<div style="display:inline-block;width:70px;height:12px;background:#e9ecef;'
                f'border-radius:3px;vertical-align:middle;overflow:hidden;">'
                f'<div style="width:{cur}%;height:12px;background:{bar_color};"></div></div>'
                f'<span style="font-size:0.85em;margin-left:6px;">{cur:.0f}%</span>'
            )

            if dr is not None:
                dr_color = "#dc3545" if dr < 7 else ("#fd7e14" if dr < 14 else "#212529")
                days_cell = f'<strong style="color:{dr_color};">{dr:.1f} days</strong>'
            else:
                days_cell = "—"

            body_rows += (
                f'<tr style="{row_bg}">'
                f'<td style="padding:6px 8px;border-bottom:1px solid #f1f3f5;">{r["printer_name"]}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #f1f3f5;">{badge} '
                f'<span style="font-size:0.85em;">{desc}</span></td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #f1f3f5;">{bar}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #f1f3f5;font-size:0.85em;">'
                f'{r.get("pct_per_day", 0):.2f}%/day</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #f1f3f5;">{days_cell}</td>'
                f'</tr>'
            )
    else:
        body_rows = (
            '<tr><td colspan="5" style="padding:18px;text-align:center;color:#6c757d;">'
            'No depleting supplies with enough data in this window.</td></tr>'
        )

    link_btn = ""
    if full_url:
        link_btn = (
            f'<p style="margin-top:18px;">'
            f'<a href="{full_url}" style="background:#0d6efd;color:#fff;padding:10px 16px;'
            f'text-decoration:none;border-radius:4px;font-weight:600;">View Full Report &amp; Charts</a>'
            f'</p>'
        )

    return f"""\
<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;color:#212529;max-width:760px;margin:0 auto;">
  <div style="background:#0d6efd;color:#fff;padding:18px 22px;border-radius:6px 6px 0 0;">
    <div style="font-size:0.85em;opacity:0.85;letter-spacing:0.05em;text-transform:uppercase;">
      {_FREQUENCY_LABEL.get(frequency, frequency.title())} Report
    </div>
    <h2 style="margin:4px 0 0;">{title}</h2>
    <div style="font-size:0.9em;opacity:0.85;margin-top:4px;">{period_label} &middot; {len(rows)} supply(ies)</div>
  </div>
  <div style="border:1px solid #dee2e6;border-top:0;padding:18px 22px;border-radius:0 0 6px 6px;">
    {banner}
    <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:0.9em;">
      <thead>
        <tr>
          <th style="text-align:left;padding:8px;border-bottom:2px solid #dee2e6;">Printer</th>
          <th style="text-align:left;padding:8px;border-bottom:2px solid #dee2e6;">Supply</th>
          <th style="text-align:left;padding:8px;border-bottom:2px solid #dee2e6;">Current</th>
          <th style="text-align:left;padding:8px;border-bottom:2px solid #dee2e6;">Rate</th>
          <th style="text-align:left;padding:8px;border-bottom:2px solid #dee2e6;">Days Left</th>
        </tr>
      </thead>
      <tbody>{body_rows}</tbody>
    </table>
    <p style="color:#6c757d;font-size:0.82em;margin-top:10px;">
      Based on linear regression of supply readings since the most recent replacement.
      Full data in the attached CSV.
    </p>
    {link_btn}
  </div>
  <p style="font-size:12px;color:#999;text-align:center;margin-top:14px;">
    Generated {generated_at} by the <strong>Network Printer Dashboard</strong>.<br>
    Configure or unsubscribe under Config &rarr; Alert Settings.
  </p>
</body></html>"""


# report key → bespoke HTML builder (else the generic _build_html is used)
_CUSTOM_HTML_BUILDERS = {
    "consumption_rate": _build_consumption_rate_html,
}


def send_one_report(report_key: str, recipient: str, frequency: str) -> tuple[bool, str]:
    """
    Send a single report on demand (e.g. from a "Send Now" test button).

    Looks up the report in REPORT_REGISTRY, fetches data with the window
    that matches `frequency`, and sends an HTML email + CSV attachment.
    """
    from app.web.routes.reports import REPORT_REGISTRY
    from app.alerts.notifier import _send_email

    report = next((r for r in REPORT_REGISTRY if r["key"] == report_key), None)
    if not report:
        return False, f"Unknown report key '{report_key}'"
    if not recipient:
        return False, "No recipient specified"

    window_days = _WINDOW_FOR_FREQUENCY.get(frequency, 7)
    try:
        rows = report["fetcher"](window_days)
    except Exception as exc:
        logger.exception("Failed to fetch %s data", report_key)
        return False, f"Failed to fetch data: {exc}"

    # Some reports get a bespoke email layout that mirrors their on-screen page.
    custom_builder = _CUSTOM_HTML_BUILDERS.get(report["key"])
    if custom_builder:
        html = custom_builder(report, rows, frequency, window_days)
    else:
        html = _build_html(report, rows, frequency, window_days)
    text = _build_text(report, rows, frequency, window_days)
    csv_bytes = _build_csv(report, rows)

    date_tag = datetime.utcnow().strftime("%Y-%m-%d")
    filename = f"{report['key']}_{date_tag}.csv"
    subject = f"[Printer Reports] {_FREQUENCY_LABEL.get(frequency, frequency.title())}: {report['title']}"

    ok, msg = _send_email(
        subject, text, html, [recipient],
        attachments=[(filename, csv_bytes, "csv")],
    )
    if ok:
        logger.info("Sent %s report (%s) to %s — %d rows",
                    report["key"], frequency, recipient, len(rows))
    else:
        logger.warning("Failed to send %s report to %s: %s",
                       report["key"], recipient, msg)
    return ok, msg


def send_scheduled_reports(frequency: str) -> None:
    """
    Called by the APScheduler cron jobs.  Iterates every report and sends
    those whose ``report_schedule_<key>`` setting matches ``frequency``.
    """
    recipient = _get_setting("report_recipient").strip()
    if not recipient:
        logger.debug("No report recipient configured; skipping %s scheduled-reports run", frequency)
        return

    from app.web.routes.reports import REPORT_REGISTRY

    sent_count = 0
    for report in REPORT_REGISTRY:
        sched_key = f"report_schedule_{report['key']}"
        if _get_setting(sched_key, "off") != frequency:
            continue
        try:
            send_one_report(report["key"], recipient, frequency)
            sent_count += 1
        except Exception:
            logger.exception("Error sending scheduled %s report", report["key"])

    logger.info("Scheduled %s reports: sent %d", frequency, sent_count)
