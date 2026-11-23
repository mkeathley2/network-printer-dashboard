"""
Polls all known printers via SNMP and writes telemetry to the database.
Called by APScheduler every N minutes.
"""
from __future__ import annotations

import logging
import socket
import threading
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
        if data.serial_number and (
            not printer.serial_number
            or _norm_serial(printer.serial_number) == _norm_serial(data.serial_number)
        ):
            # A DIFFERENT serial is never written here — see
            # _resolve_serial_change(), which decides replace vs. move.
            printer.serial_number = data.serial_number
            # Remember the standard-MIB serial when a vendor module replaced it
            generic = (getattr(data, "serial_generic", None) or "").strip()
            if generic and _norm_serial(generic) != _norm_serial(data.serial_number):
                printer.serial_alt = generic[:128]
        if data.sysname:
            printer.hostname = data.sysname
        if data.vendor != "generic":
            printer.vendor = data.vendor
    else:
        printer.consecutive_failures = min((printer.consecutive_failures or 0) + 1, 30000)


# ---------------------------------------------------------------------------
# Serial-number change handling (server-polled printers only)
# ---------------------------------------------------------------------------
# When the device answering at a printer's IP reports a different serial, the
# physical printer was swapped.  Without this, the new printer silently took
# over the old record and their toner/page history got mixed together.
#
#   * new serial belongs to another server-polled record (active OR removed)
#       -> that printer MOVED here: its record (and history) follows it to
#          this IP, and is restored if it was in Removed.
#   * new serial is unknown
#       -> a NEW printer: a fresh record is started at this IP.
#   * either way the record that used to be at this IP goes to Removed with
#     a note, since that physical printer is no longer answering here.
#
# Safety: a change is acted on only after the SAME new serial is seen on two
# consecutive polls (Printer.pending_serial), so one odd SNMP read can never
# split a history.  Remote-agent printers are excluded on purpose — many
# sites share the same private IP ranges and agents manage their own rows.

def _norm_serial(serial) -> str:
    return (serial or "").strip().upper()


def _alt_serial(data: PrinterData):
    generic = (getattr(data, "serial_generic", None) or "").strip()
    if generic and _norm_serial(generic) != _norm_serial(data.serial_number):
        return generic[:128]
    return None


def _append_note(printer: Printer, line: str) -> None:
    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    entry = f"[{stamp}] {line}"
    printer.notes = f"{printer.notes.rstrip()}\n{entry}" if printer.notes else entry


def _resolve_serial_change(printer: Printer, data: PrinterData, db_session: Session) -> Printer:
    """
    Decide which Printer record this poll result belongs to.  Returns the
    record to write telemetry to (``printer`` itself in the normal case).
    """
    if printer.agent_id is not None or not data.is_online:
        return printer
    new_serial = _norm_serial(data.serial_number)
    old_serial = _norm_serial(printer.serial_number)
    # Every identifier we know for this record vs. every identifier this poll
    # reported.  Any overlap = same physical printer.  (Vendor modules replace
    # the standard-MIB serial with their own; when that enrichment fails for a
    # poll, only the standard one comes back — still the same printer.)
    known = {old_serial, _norm_serial(printer.serial_alt)} - {""}
    reported = {new_serial, _norm_serial(getattr(data, "serial_generic", None))} - {""}
    if not new_serial or not old_serial or (known & reported):
        if printer.pending_serial:
            printer.pending_serial = None      # back to normal — forget the odd read
        return printer

    # First sighting of a different serial: park it and wait for confirmation.
    if _norm_serial(printer.pending_serial) != new_serial:
        printer.pending_serial = data.serial_number.strip()[:128]
        logger.info(
            "Printer %s: serial changed %s -> %s (first sighting; will act if the next poll agrees)",
            printer.ip_address, printer.serial_number, data.serial_number,
        )
        return printer

    # Confirmed on two consecutive polls.
    ip = printer.ip_address
    old_name = printer.effective_name
    old_serial_raw = printer.serial_number
    new_serial_raw = data.serial_number.strip()[:128]
    printer.pending_serial = None

    others = [
        o for o in db_session.query(Printer).filter(
            Printer.agent_id.is_(None), Printer.id != printer.id,
        ).all()
        if {_norm_serial(o.serial_number), _norm_serial(o.serial_alt)} & reported
    ]
    # Prefer an active match, then the most recently seen.
    _epoch = datetime(1971, 1, 1)
    others.sort(key=lambda o: (0 if o.is_active else 1,
                               -(o.last_seen_at or _epoch).timestamp()))

    # The record that was at this IP leaves the dashboard either way.
    printer.is_active = False
    printer.is_online = False

    if others:
        target = others[0]
        was_removed = not target.is_active
        old_ip = target.ip_address
        _append_note(printer, f"Auto-removed: {ip} now answers as serial {new_serial_raw} "
                              f"({target.effective_name}, moved here from {old_ip}).")
        target.ip_address = ip
        target.is_active = True
        if printer.location_id is not None:
            target.location_id = printer.location_id
        _append_note(target, f"Moved from {old_ip} to {ip} (same serial {new_serial_raw} detected)"
                             f"{'; restored from Removed' if was_removed else ''}. "
                             f"Replaced {old_name} (serial {old_serial_raw}).")
        action, detail = "printer_moved", (
            f"Serial {new_serial_raw} ({target.effective_name}) moved {old_ip} -> {ip}"
            f"{' and was restored from Removed' if was_removed else ''}; "
            f"previous printer at {ip} ({old_name}, serial {old_serial_raw}) moved to Removed")
    else:
        target = Printer(
            ip_address=ip,
            agent_id=None,
            snmp_version=printer.snmp_version,
            snmp_community=printer.snmp_community,
            snmp_v3_user=printer.snmp_v3_user,
            snmp_v3_auth_proto=printer.snmp_v3_auth_proto,
            snmp_v3_auth_key=printer.snmp_v3_auth_key,
            snmp_v3_priv_proto=printer.snmp_v3_priv_proto,
            snmp_v3_priv_key=printer.snmp_v3_priv_key,
            location_id=printer.location_id,
            group_id=printer.group_id,
            display_name=printer.display_name,
            assigned_person=printer.assigned_person,
            assigned_computer=printer.assigned_computer,
            phone_ext=printer.phone_ext,
            serial_number=new_serial_raw,
            serial_alt=_alt_serial(data),
            is_active=True,
            is_online=False,
            consecutive_failures=0,
            consecutive_misses=0,
        )
        db_session.add(target)
        _append_note(printer, f"Auto-removed: replaced at {ip} by a new printer "
                              f"(serial {new_serial_raw}).")
        _append_note(target, f"New printer detected at {ip}; replaced {old_name} "
                             f"(serial {old_serial_raw}). Location and assignment copied "
                             f"from the old record — please verify.")
        action, detail = "printer_replaced", (
            f"New printer (serial {new_serial_raw}) detected at {ip}; previous printer "
            f"{old_name} (serial {old_serial_raw}) moved to Removed and a fresh record started")

    db_session.flush()   # target needs an id before telemetry is written
    logger.info("Serial change at %s: %s", ip, detail)
    _audit_buffer.entries.append((action, ip, detail))
    return target


# Audit entries are written only AFTER the poll transaction commits (audit()
# uses its own session; writing mid-transaction could record a change that
# then rolled back).
# Thread-local: the scheduled poll and a manual "Poll Now" can overlap.
class _AuditBuffer(threading.local):
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []


_audit_buffer = _AuditBuffer()


def _flush_audits(committed: bool) -> None:
    entries, _audit_buffer.entries = _audit_buffer.entries, []
    if not committed:
        return
    try:
        from app.utils.audit import audit
        for action, target, detail in entries:
            audit("system", action, target, detail)
    except Exception:
        logger.exception("Could not write serial-change audit entries")


def _apply_poll_result(printer: Printer, data: PrinterData, db_session: Session) -> None:
    """Serial-change resolution + status + telemetry + alerts for one result."""
    target = _resolve_serial_change(printer, data, db_session)
    _update_printer_status(target, data, db_session)
    _write_telemetry(target, data, db_session)
    _run_alerts(target, data, db_session)


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
    try:
        _apply_poll_result(printer, data, db_session)
        db_session.commit()
        _flush_audits(True)
    except Exception:
        _flush_audits(False)
        raise


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
    if data is not None and printer.ip_address != data.ip_address:
        # The record was re-pointed to another IP during this batch (a moved
        # printer) — this result was probed at its OLD address. Discard it.
        return
    if data is None:
        _bump_failures(printer)
    else:
        if detected_snmp_version:
            printer.snmp_version = detected_snmp_version
        _apply_poll_result(printer, data, db_session)
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
                _flush_audits(True)
                saved += 1
                break
            except Exception:
                db_session.rollback()
                _flush_audits(False)
                if attempt == 2:
                    failed += 1
                    logger.exception("Failed to save poll result for %s", ips[pid])
                else:
                    logger.warning("Save conflict for %s — retrying once", ips[pid])

    elapsed = (datetime.utcnow() - start).total_seconds()
    logger.info("Poll complete. %d printers in %.1fs (%d saved, %d failed)",
                len(printers), elapsed, saved, failed)
