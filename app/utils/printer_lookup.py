"""
Lookup helper for server-polled ("local") printers by IP address.

Since v0.0.33 more than one server-polled Printer row can share an IP: when a
printer is replaced, the old record moves to Removed and a fresh record is
started at the same address.  Remote-agent printers can also reuse the same
private IPs.  Anything that asks "is there already a printer at this IP?"
must therefore prefer the ACTIVE server-polled record and ignore agent rows.
"""
from __future__ import annotations

from typing import Optional


def find_local_printer_by_ip(db_session, ip: str):
    """
    Return the server-polled printer at ``ip``: the active one if there is
    one, otherwise the most recently seen removed one, otherwise None.
    """
    from app.models import Printer

    rows = (
        db_session.query(Printer)
        .filter(Printer.ip_address == ip, Printer.agent_id.is_(None))
        .all()
    )
    if not rows:
        return None
    active = [p for p in rows if p.is_active]
    if active:
        return active[0]
    rows.sort(key=lambda p: (p.last_seen_at is not None, p.last_seen_at or p.added_at, p.id),
              reverse=True)
    return rows[0]
