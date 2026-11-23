"""
Daily automatic network scan for the server-polled (VPN) sites (v0.0.33).

Remote agents already rescan their own subnet on every check-in, so a new
printer at an agent site shows up by itself.  Sites the server polls directly
only ever gained printers when someone ran a manual discovery scan — a
replaced printer on a new IP stayed invisible.  This job closes that gap.

Once a day it sweeps:
  * every /24 that contains at least one active server-polled printer, plus
  * any extra subnets listed in Config (for sites with no printers yet).

Anything SNMP-responsive that is not already an active server-polled printer
goes into the ``discovered_devices`` approval queue — it is NEVER added to the
dashboard automatically, because scans also find non-printers.  An admin then
Approves it (adds/restores the printer) or Ignores it (remembered; it will
not be queued again unless un-ignored).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

AUTO_SCAN_HOUR_DEFAULT = 5          # 5 AM, site timezone
_MIN_PREFIX = 20                    # refuse anything larger than a /20 (4094 hosts)
_PENDING_EXPIRE_DAYS = 30           # drop pending entries not seen for this long

_scan_lock = threading.Lock()
_registered: dict = {}              # {"scheduler": ..., "func": ...}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _setting(key: str, default: str = "") -> str:
    try:
        from app.core.database import db
        from app.models import SiteSetting
        row = db.session.get(SiteSetting, key)
        if row and row.value is not None:
            return row.value
    except Exception:
        logger.debug("Could not read setting %s", key, exc_info=True)
    return default


def is_enabled() -> bool:
    return _setting("auto_scan_enabled", "1") == "1"


def scan_hour() -> int:
    try:
        return min(23, max(0, int(_setting("auto_scan_hour", str(AUTO_SCAN_HOUR_DEFAULT)))))
    except (TypeError, ValueError):
        return AUTO_SCAN_HOUR_DEFAULT


def parse_extra_subnets(raw: str) -> tuple[list[str], list[str]]:
    """Split a comma/space/newline separated list into (valid_cidrs, rejected)."""
    valid, rejected = [], []
    for token in (raw or "").replace(",", " ").replace(";", " ").split():
        try:
            net = ipaddress.ip_network(token, strict=False)
            if net.version != 4 or net.prefixlen < _MIN_PREFIX:
                rejected.append(token)
            else:
                valid.append(str(net))
        except ValueError:
            rejected.append(token)
    return valid, rejected


def get_scan_subnets(db_session: Session) -> list[str]:
    """Subnets the daily scan will sweep, de-duplicated, widest first."""
    from app.models import Printer

    nets: set[ipaddress.IPv4Network] = set()
    for (ip,) in db_session.query(Printer.ip_address).filter(
        Printer.is_active.is_(True), Printer.agent_id.is_(None)
    ).all():
        try:
            nets.add(ipaddress.ip_network(f"{ip}/24", strict=False))
        except ValueError:
            continue
    extras, _ = parse_extra_subnets(_setting("auto_scan_extra_subnets", ""))
    for cidr in extras:
        nets.add(ipaddress.ip_network(cidr))

    # Drop any subnet fully contained in another one on the list
    ordered = sorted(nets, key=lambda n: (n.prefixlen, int(n.network_address)))
    kept: list[ipaddress.IPv4Network] = []
    for n in ordered:
        if not any(n.subnet_of(k) for k in kept):
            kept.append(n)
    return [str(n) for n in kept]


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def _sweep(cidr: str, community: str) -> list[dict]:
    from app.core.config import config
    from app.scanner.discovery import _run_scan_async

    hosts = list(ipaddress.ip_network(cidr, strict=False).hosts())
    batch_size = max(config.polling.discovery_workers, 50)
    found: list[dict] = []
    for start in range(0, len(hosts), batch_size):
        found.extend(asyncio.run(_run_scan_async(
            hosts[start:start + batch_size],
            community,
            timeout=config.polling.discovery_timeout,
            max_concurrent=config.polling.discovery_workers,
        )))
    return found


def run_auto_scan(db_session: Session, sweep=None) -> Optional[dict]:
    """
    Run one full auto-scan.  Returns a summary dict, or None if a scan is
    already in progress.  ``sweep`` is injectable for tests.
    """
    from app.core.config import config
    from app.models import DiscoveredDevice, Printer, SiteSetting

    if not _scan_lock.acquire(blocking=False):
        logger.info("Auto-scan already running — skipping this request")
        return None
    try:
        sweep = sweep or _sweep
        subnets = get_scan_subnets(db_session)
        db_session.rollback()      # hold nothing open during the network phase
        community = config.discovery.default_community
        logger.info("Auto-scan starting: %d subnet(s)", len(subnets))
        started = datetime.utcnow()

        results: dict[str, dict] = {}       # ip -> info (+ subnet)
        failed_subnets = 0
        for cidr in subnets:
            try:
                for r in sweep(cidr, community):
                    r = dict(r)
                    r["subnet"] = cidr
                    results[r["ip"]] = r
            except Exception:
                failed_subnets += 1
                logger.exception("Auto-scan sweep failed for %s", cidr)

        # --- Persist (short transaction) ---
        now = datetime.utcnow()
        active_ips = {
            ip for (ip,) in db_session.query(Printer.ip_address).filter(
                Printer.is_active.is_(True), Printer.agent_id.is_(None)).all()
        }
        existing = {d.ip_address: d for d in db_session.query(DiscoveredDevice).all()}

        new_pending = 0
        for ip, r in results.items():
            dev = existing.get(ip)
            if ip in active_ips:
                continue
            if dev is None:
                db_session.add(DiscoveredDevice(
                    ip_address=ip,
                    hostname=(r.get("sysname") or None),
                    vendor_detected=r.get("vendor"),
                    model_detected=r.get("model"),
                    subnet=r.get("subnet"),
                    status="pending",
                    first_seen_at=now,
                    last_seen_at=now,
                ))
                new_pending += 1
            else:
                dev.last_seen_at = now
                dev.hostname = r.get("sysname") or dev.hostname
                dev.vendor_detected = r.get("vendor") or dev.vendor_detected
                dev.model_detected = r.get("model") or dev.model_detected
                dev.subnet = r.get("subnet") or dev.subnet

        # Tidy the queue: pending entries that are now active printers (added
        # some other way) or that have not answered for a month are dropped.
        expire_before = now - timedelta(days=_PENDING_EXPIRE_DAYS)
        for ip, dev in existing.items():
            if dev.status != "pending":
                continue
            if ip in active_ips or (dev.last_seen_at or dev.first_seen_at) < expire_before:
                db_session.delete(dev)

        db_session.flush()
        pending_total = db_session.query(DiscoveredDevice).filter_by(status="pending").count()
        elapsed = (datetime.utcnow() - started).total_seconds()
        summary = {
            "subnets": len(subnets),
            "failed_subnets": failed_subnets,
            "responsive": len(results),
            "new_pending": new_pending,
            "pending_total": pending_total,
            "seconds": round(elapsed, 1),
        }
        text_summary = (
            f"{len(subnets)} subnet(s) scanned in {elapsed:.0f}s — "
            f"{len(results)} device(s) answered, {new_pending} new awaiting review"
            + (f", {failed_subnets} subnet(s) failed" if failed_subnets else "")
        )
        for key, value in (("auto_scan_last_run", now.strftime("%Y-%m-%d %H:%M:%S")),
                           ("auto_scan_last_summary", text_summary)):
            row = db_session.get(SiteSetting, key)
            if row:
                row.value = value
            else:
                db_session.add(SiteSetting(key=key, value=value))
        db_session.commit()
        logger.info("Auto-scan complete: %s", text_summary)
        return summary
    except Exception:
        db_session.rollback()
        raise
    finally:
        _scan_lock.release()


# ---------------------------------------------------------------------------
# Scheduling (run.py registers; the Config page reschedules on change)
# ---------------------------------------------------------------------------
def register(scheduler, func) -> None:
    """Called once from run.py with the scheduler and the job wrapper."""
    _registered["scheduler"] = scheduler
    _registered["func"] = func
    reschedule()


def reschedule() -> None:
    """(Re)create the daily job from current settings. No-op if not registered."""
    scheduler = _registered.get("scheduler")
    func = _registered.get("func")
    if scheduler is None or func is None:
        return
    from app.utils.timezone import get_site_tz
    try:
        scheduler.add_job(
            func,
            trigger="cron",
            hour=scan_hour(),
            minute=0,
            timezone=get_site_tz(),
            id="auto_scan",
            replace_existing=True,
        )
    except Exception:
        logger.exception("Could not schedule the daily auto-scan")
