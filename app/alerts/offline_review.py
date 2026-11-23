"""
Long-offline review tickets (v0.0.33).

Two automatic helpdesk tickets, both independent of the "Printer Offline"
alert-email toggle (which most sites turn off because short outages are noisy):

1. **Printer offline for N days** (default 15) — one ticket per printer per
   outage, asking the helpdesk to review whether it was replaced or removed.
   Names a *probable* replacement when a newer printer appeared at the same
   site.  Skipped while the printer's remote agent is itself stale — in that
   case there is simply no data, and the agent ticket below covers it.

2. **Remote agent (client) not checked in for N days** (default 15) — one
   ticket per agent per outage, marked for assignment to a named person.

Also used by the manual "Offline Ticket" button and the Offline Printers report.

Concurrency note: the dedup flags are written with a bare UPDATE in a fresh
transaction (never read-modify-write on a long-lived snapshot), so MariaDB
snapshot isolation (error 1020) cannot reject them when a poll or check-in
touches the same row meanwhile.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

LONG_OFFLINE_DAYS_DEFAULT = 15
STALE_AGENT_DAYS_DEFAULT = 15
STALE_AGENT_ASSIGNEE_DEFAULT = "Matt Keathley"

# A printer first seen within this many days BEFORE the old one went dark (or
# any time after) counts as a possible replacement.
_REPLACEMENT_LOOKBACK_DAYS = 3


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _setting(key: str, default: str) -> str:
    try:
        from app.core.database import db
        from app.models import SiteSetting
        row = db.session.get(SiteSetting, key)
        if row and row.value is not None and row.value != "":
            return row.value
    except Exception:
        logger.debug("Could not read setting %s", key, exc_info=True)
    return default


def _int_setting(key: str, default: int) -> int:
    try:
        return max(0, int(_setting(key, str(default))))
    except (TypeError, ValueError):
        return default


def long_offline_days() -> int:
    """Days offline before the review ticket fires. 0 = disabled."""
    return _int_setting("long_offline_ticket_days", LONG_OFFLINE_DAYS_DEFAULT)


def stale_agent_days() -> int:
    """Days without a check-in before the agent ticket fires. 0 = disabled."""
    return _int_setting("stale_agent_ticket_days", STALE_AGENT_DAYS_DEFAULT)


def stale_agent_assignee() -> str:
    return _setting("stale_agent_ticket_assignee", STALE_AGENT_ASSIGNEE_DEFAULT).strip()


def baseline_at() -> Optional[datetime]:
    """
    Upgrade moment recorded when this feature first started (see
    _seed_offline_ticket_baseline).  Outages whose threshold was crossed
    BEFORE this are never auto-ticketed — no ticket burst for old outages.
    """
    raw = _setting("offline_ticket_baseline_at", "")
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S") if raw else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def offline_reference(printer) -> Optional[datetime]:
    """The moment we last knew this printer was alive (last seen, else added)."""
    return printer.last_seen_at or printer.added_at


def days_offline(printer, now: Optional[datetime] = None) -> Optional[float]:
    """Days since the printer was last seen online. None if it is online."""
    if printer.is_online:
        return None
    ref = offline_reference(printer)
    if ref is None:
        return None
    return max(0.0, ((now or datetime.utcnow()) - ref).total_seconds() / 86400.0)


def agent_is_stale(agent) -> bool:
    """True when the agent is flagged stale (no check-in within 2x its interval)."""
    return agent is not None and agent.status == "stale"


def _same_slash24(ip_a: str, ip_b: str) -> bool:
    try:
        return (ipaddress.ip_network(f"{ip_a}/24", strict=False)
                == ipaddress.ip_network(f"{ip_b}/24", strict=False))
    except ValueError:
        return False


def find_probable_replacements(printer, db_session, limit: int = 3) -> list:
    """
    Printers that MAY have replaced ``printer``: active, currently online,
    at the same site, and first added around/after the time the old one was
    last seen.  This is a hint only — never proof of a replacement.

    "Same site" = same remote agent for agent-reported printers; for
    server-polled printers, same Location when one is set, else same /24.
    """
    from app.models import Printer

    ref = offline_reference(printer)
    if ref is None:
        return []
    since = ref - timedelta(days=_REPLACEMENT_LOOKBACK_DAYS)

    q = db_session.query(Printer).filter(
        Printer.is_active.is_(True),
        Printer.is_online.is_(True),
        Printer.id != printer.id,
        Printer.added_at >= since,
    )
    if printer.agent_id is not None:
        q = q.filter(Printer.agent_id == printer.agent_id)
        candidates = q.all()
    else:
        q = q.filter(Printer.agent_id.is_(None))
        if printer.location_id is not None:
            candidates = q.filter(Printer.location_id == printer.location_id).all()
        else:
            candidates = [c for c in q.all() if _same_slash24(c.ip_address, printer.ip_address)]

    candidates.sort(key=lambda c: abs((c.added_at - ref).total_seconds()))
    return candidates[:limit]


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "never"
    try:
        from app.utils.timezone import to_local
        local = to_local(dt)
        if local:
            return local.strftime("%Y-%m-%d %H:%M %Z").strip()
    except Exception:
        pass
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _latest_supplies(printer, db_session) -> list:
    from app.models import SupplySnapshot, TelemetrySnapshot
    latest = (
        db_session.query(TelemetrySnapshot)
        .filter_by(printer_id=printer.id)
        .order_by(TelemetrySnapshot.polled_at.desc())
        .first()
    )
    if not latest:
        return []
    return (
        db_session.query(SupplySnapshot)
        .filter_by(telemetry_id=latest.id)
        .order_by(SupplySnapshot.supply_index)
        .all()
    )


# ---------------------------------------------------------------------------
# Printer offline review ticket
# ---------------------------------------------------------------------------
def build_offline_note(printer, db_session, automatic: bool) -> str:
    d = days_offline(printer)
    days_txt = f"{d:.0f} day(s)" if d is not None else "an unknown time"
    source = (f"remote client '{printer.agent.name}'" if printer.agent_id and printer.agent
              else "the dashboard server (VPN)")
    lines = [
        ("AUTOMATIC TICKET: printer offline — please review." if automatic
         else "Printer offline — please review."),
        "",
        f"Offline for:   {days_txt}",
        f"Last seen:     {_fmt_dt(printer.last_seen_at)}",
        f"Serial number: {printer.serial_number or 'unknown'}",
        f"Monitored by:  {source}",
        "",
        "Please check whether this printer was replaced, moved, or removed from",
        "service. If it is gone for good, remove it from the dashboard (open the",
        "printer and click Remove) so it stops showing as offline.",
    ]
    candidates = find_probable_replacements(printer, db_session)
    if candidates:
        lines += [
            "",
            "Possible replacement (NOT confirmed — newer printer seen at the same site):",
        ]
        for c in candidates:
            lines.append(
                f"  - {c.effective_name} ({c.ip_address})"
                f"{' — ' + c.model if c.model else ''}"
                f"{', serial ' + c.serial_number if c.serial_number else ''}"
                f", first seen {_fmt_dt(c.added_at)}"
            )
    if automatic:
        lines += [
            "",
            "You will not get another automatic ticket for this printer unless it",
            "comes back online and goes offline again.",
        ]
    return "\n".join(lines)


def send_offline_review_ticket(printer, db_session, sent_by: str, automatic: bool) -> tuple[bool, str]:
    """Send the offline review ticket for one printer. Does not touch dedup flags."""
    from app.alerts.notifier import send_helpdesk_ticket
    d = days_offline(printer)
    days_part = f"{d:.0f} days" if d is not None else "extended time"
    subject = (f"[Printer Ticket] Offline {days_part} — please review: "
               f"{printer.effective_name} ({printer.ip_address})")
    note = build_offline_note(printer, db_session, automatic)
    return send_helpdesk_ticket(printer, _latest_supplies(printer, db_session), note, sent_by,
                                subject=subject)


# ---------------------------------------------------------------------------
# Stale remote agent ticket
# ---------------------------------------------------------------------------
def send_stale_agent_ticket(agent, printer_count: int) -> tuple[bool, str]:
    """Helpdesk ticket: a remote agent (client) has not checked in for N days."""
    from app.alerts.notifier import _send_email
    helpdesk_email = _setting("helpdesk_email", "")
    if not helpdesk_email:
        return False, "No helpdesk email address configured."

    now = datetime.utcnow()
    days = ((now - agent.last_checkin_at).total_seconds() / 86400.0
            if agent.last_checkin_at else 0.0)
    assignee = stale_agent_assignee()
    assign_line = f"Please assign this ticket to {assignee}." if assignee else ""
    loc_name = agent.location.name if agent.location else "No location"
    last_seen = _fmt_dt(agent.last_checkin_at)

    subject = f"[Printer Ticket] Remote client offline {days:.0f} days: {agent.name}"
    if assignee:
        subject += f" — assign to {assignee}"

    agents_link = ""
    try:
        from app.utils.dashboard_url import get_dashboard_url
        base = get_dashboard_url()
        if base:
            agents_link = f"{base}/config/?tab=agents"
    except Exception:
        pass

    body_text = (
        "AUTOMATIC TICKET: remote printer client has stopped checking in.\n\n"
        + (f"*** {assign_line} ***\n\n" if assign_line else "")
        + f"Client:          {agent.name}\n"
        f"Location:        {loc_name}\n"
        f"Last check-in:   {last_seen}  ({days:.0f} days ago)\n"
        f"Client version:  {agent.agent_version or 'unknown'}\n"
        f"Last known IP:   {agent.last_ip or 'unknown'}\n"
        f"Subnet:          {agent.subnet or 'auto-detect'}\n"
        f"Printers behind this client: {printer_count}\n\n"
        "No printer data is arriving from this site. The station PC may have been\n"
        "replaced, re-imaged, or powered off, in which case the client needs to be\n"
        "reinstalled (Config -> Remote Agents has the install command).\n"
        + (f"\nRemote Agents page: {agents_link}\n" if agents_link else "")
        + "\nYou will not get another ticket for this client unless it checks in and\n"
        "then goes quiet again.\n\n"
        "This ticket was created automatically by the Network Printer Dashboard.\n"
    )
    assign_html = (f'<p style="background:#fff3cd;padding:10px;border-left:4px solid #ffc107;">'
                   f'<strong>{assign_line}</strong></p>' if assign_line else "")
    link_html = (f'<p><a href="{agents_link}" style="background:#0d6efd;color:#fff;padding:8px 14px;'
                 f'text-decoration:none;border-radius:4px;font-weight:600;">Open Remote Agents</a></p>'
                 if agents_link else "")
    body_html = f"""\
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;">
  <h2 style="color:#c0392b;">Remote Client Offline {days:.0f} Days</h2>
  {assign_html}
  <table cellpadding="6" cellspacing="0" border="0" style="border-collapse:collapse;">
    <tr><td><strong>Client</strong></td><td>{agent.name}</td></tr>
    <tr><td><strong>Location</strong></td><td>{loc_name}</td></tr>
    <tr><td><strong>Last check-in</strong></td><td>{last_seen} ({days:.0f} days ago)</td></tr>
    <tr><td><strong>Client version</strong></td><td>{agent.agent_version or 'unknown'}</td></tr>
    <tr><td><strong>Last known IP</strong></td><td>{agent.last_ip or 'unknown'}</td></tr>
    <tr><td><strong>Subnet</strong></td><td>{agent.subnet or 'auto-detect'}</td></tr>
    <tr><td><strong>Printers behind this client</strong></td><td>{printer_count}</td></tr>
  </table>
  <p>No printer data is arriving from this site. The station PC may have been replaced,
  re-imaged, or powered off, in which case the client needs to be reinstalled
  (Config &rarr; Remote Agents has the install command).</p>
  {link_html}
  <hr/>
  <p style="font-size:12px;color:#999;">Created automatically by the
  <strong>Network Printer Dashboard</strong>. One ticket per outage.</p>
</body></html>"""
    return _send_email(subject, body_text, body_html, [helpdesk_email])


# ---------------------------------------------------------------------------
# Dedup flag writers (fresh short transaction — see module docstring)
# ---------------------------------------------------------------------------
def _exec_update(db_session, sql: str, params: dict) -> None:
    from sqlalchemy import text
    for attempt in (1, 2):
        try:
            db_session.rollback()            # drop any old snapshot first
            db_session.execute(text(sql), params)
            db_session.commit()
            return
        except Exception:
            db_session.rollback()
            if attempt == 2:
                raise


def mark_offline_ticket_sent(printer_id: int, db_session, sent: bool = True) -> None:
    _exec_update(db_session,
                 "UPDATE printers SET offline_ticket_sent = :v WHERE id = :id",
                 {"v": 1 if sent else 0, "id": printer_id})


def mark_stale_ticket_sent(agent_id: int, db_session, sent: bool = True) -> None:
    _exec_update(db_session,
                 "UPDATE remote_agents SET stale_ticket_sent = :v WHERE id = :id",
                 {"v": 1 if sent else 0, "id": agent_id})


# ---------------------------------------------------------------------------
# Scheduled job body
# ---------------------------------------------------------------------------
def run_offline_review(db_session) -> dict:
    """
    Hourly job. Returns counters for logging/tests:
    {"printer_tickets", "agent_tickets", "reset_printers", "reset_agents"}.
    """
    from sqlalchemy import text
    from app.models import Printer, RemoteAgent

    stats = {"printer_tickets": 0, "agent_tickets": 0, "reset_printers": 0, "reset_agents": 0}
    now = datetime.utcnow()

    # 1) Re-arm: a printer that is online again / an agent that has checked in
    #    again gets a fresh ticket budget for its next outage.
    try:
        db_session.rollback()
        r1 = db_session.execute(text(
            "UPDATE printers SET offline_ticket_sent = 0 "
            "WHERE offline_ticket_sent = 1 AND is_online = 1"))
        recent = now - timedelta(days=1)
        r2 = db_session.execute(text(
            "UPDATE remote_agents SET stale_ticket_sent = 0 "
            "WHERE stale_ticket_sent = 1 AND last_checkin_at >= :recent"), {"recent": recent})
        db_session.commit()
        stats["reset_printers"] = r1.rowcount or 0
        stats["reset_agents"] = r2.rowcount or 0
    except Exception:
        db_session.rollback()
        logger.exception("Offline-review re-arm step failed")

    if not _setting("helpdesk_email", ""):
        return stats   # nowhere to send tickets — try again next hour

    baseline = baseline_at()

    # 2) Stale remote agents
    a_days = stale_agent_days()
    if a_days > 0:
        cutoff = now - timedelta(days=a_days)
        agent_ids = [a.id for a in db_session.query(RemoteAgent).filter(
            RemoteAgent.stale_ticket_sent.is_(False),
            RemoteAgent.last_checkin_at.isnot(None),
            RemoteAgent.last_checkin_at <= cutoff,
        ).all()
            # crossed the threshold before the upgrade -> never auto-ticketed
            if baseline is None or a.last_checkin_at + timedelta(days=a_days) >= baseline]
        for agent_id in agent_ids:
            try:
                agent = db_session.get(RemoteAgent, agent_id)
                if agent is None:
                    continue
                pcount = db_session.query(Printer).filter_by(
                    agent_id=agent_id, is_active=True).count()
                ok, msg = send_stale_agent_ticket(agent, pcount)
                name = agent.name
                if ok:
                    mark_stale_ticket_sent(agent_id, db_session)
                    stats["agent_tickets"] += 1
                    logger.info("Stale-client ticket sent for agent '%s'", name)
                else:
                    logger.warning("Stale-client ticket failed for agent '%s': %s", name, msg)
            except Exception:
                db_session.rollback()
                logger.exception("Stale-client ticket error for agent id %s", agent_id)

    # 3) Long-offline printers
    p_days = long_offline_days()
    if p_days > 0:
        cutoff = now - timedelta(days=p_days)
        db_session.rollback()
        candidates = db_session.query(Printer).filter(
            Printer.is_active.is_(True),
            Printer.is_online.is_(False),
            Printer.offline_ticket_sent.is_(False),
        ).all()
        printer_ids = []
        for p in candidates:
            ref = offline_reference(p)
            if ref is None or ref > cutoff:
                continue
            if baseline is not None and ref + timedelta(days=p_days) < baseline:
                continue   # crossed the threshold before the upgrade
            if p.agent_id is not None and agent_is_stale(p.agent):
                continue   # no data from that site at all — the agent ticket covers it
            printer_ids.append(p.id)
        for pid in printer_ids:
            try:
                printer = db_session.get(Printer, pid)
                if printer is None or printer.is_online or not printer.is_active:
                    continue
                ok, msg = send_offline_review_ticket(
                    printer, db_session, "system/long-offline", automatic=True)
                label = f"{printer.effective_name} ({printer.ip_address})"
                if ok:
                    mark_offline_ticket_sent(pid, db_session)
                    stats["printer_tickets"] += 1
                    logger.info("Long-offline review ticket sent for %s", label)
                else:
                    logger.warning("Long-offline ticket failed for %s: %s", label, msg)
            except Exception:
                db_session.rollback()
                logger.exception("Long-offline ticket error for printer id %s", pid)

    return stats
