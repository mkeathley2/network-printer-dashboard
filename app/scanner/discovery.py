"""
CIDR range discovery scanner.
Uses asyncio with a semaphore to probe all IPs concurrently — much faster than
the thread-per-IP approach since SNMP is I/O bound.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import config
from app.models import DiscoveryScan, DiscoveryResult, Printer
from app.snmp import oids
from app.snmp.client import _build_auth, _coerce_value

logger = logging.getLogger(__name__)


async def _probe_ip_async(
    ip: str,
    community: str,
    timeout: int,
    semaphore: asyncio.Semaphore,
) -> Optional[dict]:
    """Probe a single IP for SNMP responsiveness. Returns info dict or None."""
    from pysnmp.hlapi.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType,
        SnmpEngine, UdpTransportTarget, get_cmd,
    )
    from app.snmp.vendor.generic import _detect_vendor

    async with semaphore:
        try:
            engine = SnmpEngine()
            auth = CommunityData(community, mpModel=1)
            transport = await UdpTransportTarget.create(
                (ip, 161), timeout=timeout, retries=0
            )
            error_indication, error_status, _, var_binds = await get_cmd(
                engine,
                auth,
                transport,
                ContextData(),
                ObjectType(ObjectIdentity(oids.SYSDESCR)),
                ObjectType(ObjectIdentity(oids.SYSOID)),
                ObjectType(ObjectIdentity(oids.SYSNAME)),
            )
            if error_indication or error_status:
                return None

            values = {}
            for vb in var_binds:
                try:
                    values[str(vb[0])] = vb[1].prettyPrint()
                except Exception:
                    values[str(vb[0])] = str(vb[1])

            sysdescr = next((v for k, v in values.items() if oids.SYSDESCR.lstrip('.') in k.lstrip('.')), None)
            sysoid   = next((v for k, v in values.items() if oids.SYSOID.lstrip('.')   in k.lstrip('.')), None)
            sysname  = next((v for k, v in values.items() if oids.SYSNAME.lstrip('.')  in k.lstrip('.')), None)

            if not sysdescr:
                return None

            vendor = _detect_vendor(sysoid, sysdescr)
            model = _extract_model_from_descr_inline(sysdescr)

            return {"ip": ip, "vendor": vendor, "model": model, "sysname": sysname}

        except Exception:
            return None


def _extract_model_from_descr_inline(sysdescr: Optional[str]) -> Optional[str]:
    if not sysdescr:
        return None
    if "PID:" in sysdescr:
        return sysdescr.split("PID:")[-1].strip()[:255]
    return sysdescr.strip().split("\n")[0].strip()[:255]


async def _run_scan_async(
    hosts: list,
    community: str,
    timeout: int,
    max_concurrent: int,
) -> list[dict]:
    """Probe all hosts concurrently. Returns list of responsive host dicts."""
    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [_probe_ip_async(str(ip), community, timeout, semaphore) for ip in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


def _update_scan_progress(scan_id: int, probed: int) -> None:
    """Write probed count to DB so the UI can show progress. Best-effort."""
    try:
        from app.core.database import get_db
        with get_db() as sess:
            s = sess.get(DiscoveryScan, scan_id)
            if s:
                s.hosts_probed = probed
    except Exception:
        pass


def run_cidr_discovery(cidr: str, community: str, scan_id: int, db_session: Session) -> None:
    """
    Scan all IPs in the given CIDR range and record results.
    Updates the DiscoveryScan row (must already exist).
    """
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        logger.error("Invalid CIDR range %r: %s", cidr, e)
        _fail_scan(scan_id, db_session)
        return

    hosts = list(network.hosts())
    total = len(hosts)
    logger.info("Starting CIDR discovery scan %d: %s (%d hosts)", scan_id, cidr, total)

    # Scan in batches so we can update the probed counter between batches
    batch_size = max(config.polling.discovery_workers, 50)
    responsive: list[dict] = []

    for batch_start in range(0, total, batch_size):
        batch = hosts[batch_start : batch_start + batch_size]
        batch_results = asyncio.run(
            _run_scan_async(
                batch,
                community,
                timeout=config.polling.discovery_timeout,
                max_concurrent=config.polling.discovery_workers,
            )
        )
        responsive.extend(batch_results)
        probed_so_far = min(batch_start + len(batch), total)
        _update_scan_progress(scan_id, probed_so_far)
        logger.debug("Scan %d progress: %d/%d probed, %d found so far",
                     scan_id, probed_so_far, total, len(responsive))

    found = 0
    for result in responsive:
        ip = result["ip"]
        found += 1
        existing = db_session.query(Printer).filter_by(ip_address=ip).first()
        already_known = existing is not None

        # Only add active printers as "already known"
        if existing and not existing.is_active:
            existing = None
            already_known = False

        dr = DiscoveryResult(
            scan_id=scan_id,
            ip_address=ip,
            hostname=result.get("sysname"),
            vendor_detected=result.get("vendor"),
            model_detected=result.get("model"),
            snmp_responsive=True,
            already_known=already_known,
            printer_id=existing.id if existing else None,
        )
        db_session.add(dr)

    try:
        db_session.flush()
    except Exception:
        db_session.rollback()

    scan = db_session.get(DiscoveryScan, scan_id)
    if scan:
        scan.status = "complete"
        scan.finished_at = datetime.utcnow()
        scan.hosts_probed = total
        scan.hosts_found = found

    logger.info("Discovery scan %d complete: %d/%d hosts found SNMP-responsive", scan_id, found, total)


def _fail_scan(scan_id: int, db_session: Session) -> None:
    scan = db_session.get(DiscoveryScan, scan_id)
    if scan:
        scan.status = "failed"
        scan.finished_at = datetime.utcnow()


def add_manual_printer(
    ip: str,
    display_name: Optional[str],
    community: str,
    db_session: Session,
) -> Printer:
    from app.snmp.vendor.generic import probe as snmp_probe
    printer = Printer(
        ip_address=ip,
        display_name=display_name,
        snmp_community=community,
    )
    db_session.add(printer)
    db_session.flush()

    snmp_params = {"version": "2c", "community": community}
    data = snmp_probe(ip, snmp_params, timeout=config.snmp.timeout, retries=config.snmp.retries)

    if data.is_online:
        printer.is_online = True
        printer.last_seen_at = datetime.utcnow()
        printer.vendor = data.vendor
        if data.model:
            printer.model = data.model
        if data.serial_number:
            printer.serial_number = data.serial_number
        if data.sysname:
            printer.hostname = data.sysname

    return printer
