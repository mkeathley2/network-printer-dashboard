"""
Polls all known printers via SNMP and writes telemetry to the database.
Called by APScheduler every N minutes.
"""
from __future__ import annotations

import logging
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
        printer.consecutive_failures += 1


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


def poll_all_printers(db_session: Session) -> None:
    """Poll all active printers concurrently. Called by APScheduler."""
    printers = db_session.query(Printer).filter_by(is_active=True).all()
    if not printers:
        logger.info("No active printers to poll.")
        return

    logger.info("Starting poll of %d printers with %d workers", len(printers), config.polling.poll_workers)
    start = datetime.utcnow()

    # We can't share a single db_session across threads safely.
    # Instead, collect results in thread and write in main thread.
    results: dict[int, PrinterData] = {}

    with ThreadPoolExecutor(max_workers=config.polling.poll_workers) as executor:
        future_to_printer = {executor.submit(_probe_printer, p): p for p in printers}
        for future in as_completed(future_to_printer):
            printer = future_to_printer[future]
            try:
                results[printer.id] = future.result()
            except Exception:
                logger.exception("Probe failed for %s", printer.ip_address)
                results[printer.id] = None  # type: ignore

    # Write all results sequentially in the main thread
    for printer in printers:
        data = results.get(printer.id)
        if data is None:
            printer.consecutive_failures += 1
            continue
        try:
            _update_printer_status(printer, data, db_session)
            _write_telemetry(printer, data, db_session)
            _run_alerts(printer, data, db_session)
        except Exception:
            logger.exception("Failed to write telemetry for %s", printer.ip_address)

    elapsed = (datetime.utcnow() - start).total_seconds()
    logger.info("Poll complete. %d printers in %.1fs", len(printers), elapsed)
