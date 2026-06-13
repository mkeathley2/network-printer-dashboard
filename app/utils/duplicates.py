"""
Potential-duplicate-printer detection.

The same physical printer can appear on the dashboard more than once — added
locally AND found by a remote agent, or re-discovered at a new IP after a DHCP
change. Serial number is the reliable key (names and IPs can legitimately
differ or collide), so we group active printers by their normalized serial.
"""
from __future__ import annotations

from datetime import datetime


def group_duplicate_printers(printers: list) -> list[list]:
    """
    Group the given printers by normalized serial number, returning only the
    groups with two or more members (the potential duplicates).

    Each group is sorted by ``last_seen_at`` descending, so the
    most-recently-polled printer (usually the one to keep) is first.
    Groups are returned ordered by serial for a stable display.
    """
    by_serial: dict[str, list] = {}
    for p in printers:
        serial = (p.serial_number or "").strip()
        if not serial:
            continue
        by_serial.setdefault(serial.upper(), []).append(p)

    groups: list[list] = []
    for serial_key in sorted(by_serial):
        members = by_serial[serial_key]
        if len(members) > 1:
            members.sort(key=lambda p: p.last_seen_at or datetime.min, reverse=True)
            groups.append(members)
    return groups
