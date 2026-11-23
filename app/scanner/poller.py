"""
Polls all known printers via SNMP and writes telemetry to the database.
Called by APScheduler every N minutes.
"""
from __future__ import annotations

import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import config
from app.models import AlertState, Printer, SupplySnapshot, TelemetrySnapshot
from app.snmp.normalizer import PrinterData, SupplyData
from app.snmp.vendor import generic as generic_probe

logger = logging.getLogger(__name__)

# Vendor enrichment module dispatch
_VENDOR_ENRICH = {}
try:
    from app.snmp.vendor import hp
    _VENDOR_ENRICH["hp"] = hp.enrich
except ImportError:
    pass
try:
    from app.snmp.vendor import brother
    _VENDOR_ENRICH["brother"] = brother.enrich
except ImportError:
    pass
try:
    from app.snmp.vendor import canon
    _VENDOR_ENRICH["canon"] = canon.enrich
except ImportError:
    pass
try:
    from app.snmp.vendor import kyocera
    _VENDOR_ENRICH["kyocera"] = kyocera.enrich
except ImportError:
    pass
try:
    from app.snmp.vendor import ricoh
    _VENDOR_ENRICH["ricoh"] = ricoh.enrich
except ImportError:
    pass


def _http_reachable(ip: str, timeout: int = 3) -> bool:
    """
    Check if TCP port 80 on the printer is accepting connections.
    Kyocera (and most MFPs) keep their HTTP server active even in deep sleep,
    so a successful TCP connect means the printer is on the network — it may
    just need a moment to wake its SNMP agent.
    """
    try:
        with socket.create_connection((ip, 80), timeout=timeout):
            return True
    except Exception:
        return False


def _build_snmp_params(printer: Printer) -> dict:
    return {
        "version": printer.snmp_version,
        "community": printer.snmp_community,
        # v3 fields (ignored for v2c)
        "user": printer.snmp_v3_user,
        "auth_proto": printer.snmp_v3_auth_proto,
        "auth_key": printer.snmp_v3_auth_key,
        "priv_proto": printer.snmp_v3_priv_proto,
        "priv_key": printer.snmp_v3_priv_key,
    }


def _probe_printer(printer: Printer) -> PrinterData:
    """Run generic + vendor-specific SNMP probe for one printer."""
    snmp_params = _build_snmp_params(printer)
    data = generic_probe.probe(
        printer.ip_address,
        snmp_params,
        timeout=config.snmp.timeout,
        retries=config.snmp.retries,
    )

    # If v2c probe got no response, retry with v1 (some devices, e.g. Canon MF series,
    # only support SNMPv1). If v1 succeeds, save the version so future polls skip the retry.
    if not data.is_online and snmp_params.get("version") == "2c":
        snmp_params_v1 = {**snmp_params, "version": "1"}
        data_v1 = generic_probe.probe(
            printer.ip_address,
            snmp_params_v1,
            timeout=config.snmp.timeout,
            retries=config.snmp.retries,
        )
        if data_v1.is_online:
            data = data_v1
            printer.snmp_version = "1"
            logger.info("Printer %s responded to SNMPv1; saved version.", printer.ip_address)

    # --- HTTP wake + SNMP retry ---
    # If SNMP failed but port 80 responds, the printer is likely in deep sleep
    # (common on Kyocera MFPs). Opening a TCP connection to port 80 wakes the
    # printer's main CPU; we then wait 10 seconds and retry SNMP once.
    if not data.is_online and _http_reachable(printer.ip_address):
        logger.info(
            "Printer %s: SNMP failed but port 80 is open — waking and retrying SNMP in 10s",
            printer.ip_address,
        )
        time.sleep(10)
        data_retry = generic_probe.probe(
            printer.ip_address,
            snmp_params,
            timeout=config.snmp.timeout,
            retries=config.snmp.retries,
        )
        if data_retry.is_online:
            logger.info("Printer %s: SNMP succeeded after wake.", printer.ip_address)
            data = data_retry

    # Apply vendor enrichment
    if data.is_online and data.vendor in _VENDOR_ENRICH:
        try:
            _VENDOR_ENRICH[data.vendor](
                data, snmp_params,
                timeout=config.snmp.timeout,
                retries=config.snmp.retries,
            )
        except Exception:
            logger.debug("Vendor enrich failed for %s", printer.ip_address, exc_info=True)
    return data


def _write_telemetry(printer: Printer, data: PrinterData, db_session: Session) -> TelemetrySnapshot:
    """Persist a TelemetrySnapshot + SupplySnapshot rows."""
    snapshot = TelemetrySnapshot(
        printer_id=printer.id,
        polled_at=datetime.utcnow(),
        is_online=data.is_online,
        page_count=data.page_count,
        uptime_seconds=data.uptime_seconds,
        status_raw=data.status_raw,
        error_state_raw=data.error_state_raw,
    )
    db_session.add(snapshot)
    db_session.flush()  # get snapshot.id

    for supply in data.supplies:
        ss = SupplySnapshot(
            telemetry_id=snapshot.id,
            printer_id=printer.id,
            polled_at=snapshot.polled_at,
            supply_index=supply.supply_index,
            supply_type=supply.supply_type,
            supply_color=supply.supply_color,
            supply_description=supply.description,
            level_current=supply.level_current,
            level_max=supply.level_max,
            level_pct=supply.level_pct,
        )
        db_session.add(ss)

    return snapshot


def _update_printer_status(printer: Printer, data: PrinterData, db_session: Session) -> None:
    """Update printer online status and model/serial if newly discovered."""
    printer.is_online = data.is_online

    if data.is_online:
        printer.consecutive_failures = 0
        printer.last_seen_at = datetime.utcnow()
        # Always update model/serial/hostname so vendor enrichment improvements
        # take effect on next poll (don't lock in stale sysDescr values)
        if data.model:
            printer.model = data.model
        if data.serial_number:
            printer.serial_number = data.serial_number
        if data.sysname:
            printer.hostname = data.sysname
        if data.vendor != "generic":
            printer.vendor = data.vendor
    else:
        printer.consecutive_failures = min((printer.consecutive_failures or 0) + 1, 30000)


def _run_alerts(printer: Printer, data: PrinterData, db_session: Session) -> None:
    """Evaluate alert thresholds and send notifications as needed."""
    try:
        from app.alerts.evaluator import evaluate
        evaluate(printer, data, db_session)
    except Exception:
        logger.exception("Alert evaluation failed for printer %s", printer.ip_address)


def poll_single_printer(printer_id: int, db_session: Session) -> None:
    """Poll one printer by ID. Used for manual one-shot polls."""
    printer = db_session.get(Printer, printer_id)
    if not printer or not printer.is_active:
        return

    logger.info("Polling printer %s (%s)", printer.ip_address, printer.effective_name)
    data = _probe_printer(printer)
    _update_printer_status(printer, data, db_session)
    _write_telemetry(printer, data, db_session)
    _run_alerts(printer, data, db_session)


# consecutive_failures is a SMALLINT (max 32767) — never let it overflow.
_MAX_FAILURES = 30000


def _bump_failures(printer: Printer) -> None:
    printer.consecutive_failures = min((printer.consecutive_failures or 0) + 1, _MAX_FAILURES)


def _save_poll_result(printer_id: int, data: PrinterData | None,
                      detected_snmp_version: str | None, db_session: Session) -> None:
    """
    Persist one printer's poll result in its OWN short transaction.

    The row is re-read fresh here (after the slow SNMP probe has finished) so
    the transaction never spans the probe.  A failure rolls back only this
    printer — it can never poison the rest of the batch.
    """
    printer = db_session.get(Printer, printer_id)
    if not printer or not printer.is_active or printer.agent_id is not None:
        return
    if data is None:
        _bump_failures(printer)
    else:
        if detected_snmp_version:
            printer.snmp_version = detected_snmp_version
        _update_printer_status(printer, data, db_session)
        _write_telemetry(printer, data, db_session)
        _run_alerts(printer, data, db_session)
    db_session.commit()


def poll_all_printers(db_session: Session) -> None:
    """
    Poll all active LOCAL printers concurrently. Called by APScheduler and by
    the dashboard's Poll All button.

    Remote-agent printers (agent_id set) are skipped: the server cannot reach
    them, and their agents report them via /api/agent/checkin.

    v0.0.32: this used to hold ONE transaction open across the whole poll
    (several minutes of SNMP probing).  MariaDB's snapshot isolation rejects
    an UPDATE to any row another transaction changed in the meantime (error
    1020 "Record has changed since last read") — e.g. an agent check-in — and
    that single failure rolled back the entire batch.  Now nothing is held
    open while probing and every printer is saved in its own transaction.
    """
    printers = (
        db_session.query(Printer)
        .filter(Printer.is_active.is_(True), Printer.agent_id.is_(None))
        .all()
    )
    if not printers:
        logger.info("No active local printers to poll.")
        return

    # Detach the loaded rows and end the read transaction BEFORE probing, so
    # no DB snapshot/connection is held during the slow network phase.  The
    # probe threads work on these detached copies only.
    original_versions = {p.id: p.snmp_version for p in printers}
    ips = {p.id: p.ip_address for p in printers}
    db_session.expunge_all()
    db_session.rollback()

    logger.info("Starting poll of %d printers with %d workers", len(printers), config.polling.poll_workers)
    start = datetime.utcnow()

    results: dict[int, PrinterData | None] = {}
    with ThreadPoolExecutor(max_workers=config.polling.poll_workers) as executor:
        future_to_printer = {executor.submit(_probe_printer, p): p for p in printers}
        for future in as_completed(future_to_printer):
            printer = future_to_printer[future]
            try:
                results[printer.id] = future.result()
            except Exception:
                logger.exception("Probe failed for %s", ips[printer.id])
                results[printer.id] = None

    # Save sequentially, one short transaction per printer.
    saved = failed = 0
    for printer in printers:
        pid = printer.id
        data = results.get(pid)
        # _probe_printer may have discovered the device only speaks SNMPv1
        detected = printer.snmp_version if printer.snmp_version != original_versions[pid] else None
        for attempt in (1, 2):
            try:
                _save_poll_result(pid, data, detected, db_session)
                saved += 1
                break
            except Exception:
                db_session.rollback()
                if attempt == 2:
                    failed += 1
                    logger.exception("Failed to save poll result for %s", ips[pid])
                else:
                    logger.warning("Save conflict for %s — retrying once", ips[pid])

    elapsed = (datetime.utcnow() - start).total_seconds()
    logger.info("Poll complete. %d printers in %.1fs (%d saved, %d failed)",
                len(printers), elapsed, saved, failed)
