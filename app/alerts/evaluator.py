"""
Alert state machine.
Called after every printer poll to evaluate thresholds and detect replacements.
One-shot email guarantee: emails are only sent once per lifecycle event.
Lifecycle resets when a replacement is detected (level jumps up).
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import config
from app.models import AlertEvent, AlertState, Printer
from app.snmp.normalizer import PrinterData, SupplyData

logger = logging.getLogger(__name__)

# supply_index sentinel for device-level (offline) alerts
DEVICE_SUPPLY_INDEX = -1


def _get_or_create_state(printer_id: int, supply_index: int, db_session: Session) -> AlertState:
    state = (
        db_session.query(AlertState)
        .filter_by(printer_id=printer_id, supply_index=supply_index)
        .first()
    )
    if state is None:
        state = AlertState(
            printer_id=printer_id,
            supply_index=supply_index,
            alert_level="none",
            email_sent_warning=False,
            email_sent_critical=False,
        )
        db_session.add(state)
        db_session.flush()
    return state


def _log_event(
    printer_id: int,
    event_type: str,
    supply: SupplyData | None,
    level_pct: int | None,
    email_sent: bool,
    db_session: Session,
) -> AlertEvent:
    event = AlertEvent(
        printer_id=printer_id,
        event_type=event_type,
        supply_index=supply.supply_index if supply else None,
        supply_color=supply.supply_color if supply else None,
        level_pct_at_event=level_pct,
        email_sent=email_sent,
        email_sent_at=datetime.utcnow() if email_sent else None,
        occurred_at=datetime.utcnow(),
    )
    db_session.add(event)
    return event


def _send(event_type: str, printer: Printer, supply: SupplyData | None, level_pct: int | None) -> bool:
    """Attempt to send an alert email. Returns True if sent successfully."""
    try:
        from app.alerts.notifier import is_alert_enabled, send_alert_email
        if not is_alert_enabled(event_type):
            logger.debug("Alert type '%s' is disabled — skipping email for %s", event_type, printer.ip_address)
            return False
        send_alert_email(event_type, printer, supply, level_pct)
        return True
    except Exception:
        logger.exception("Failed to send alert email for printer %s event %s", printer.ip_address, event_type)
        return False


def _evaluate_supply(printer: Printer, supply: SupplyData, db_session: Session) -> None:
    """Evaluate a single supply's alert state."""
    if supply.level_pct is None:
        return

    cfg = config.alerts
    state = _get_or_create_state(printer.id, supply.supply_index, db_session)
    current_pct = supply.level_pct
    previous_pct = state.last_level_pct

    just_replaced = False

    # --- Replacement detection ---
    if (
        previous_pct is not None
        and current_pct >= previous_pct + cfg.replacement_jump_threshold
    ):
        just_replaced = True
        logger.info(
            "Replacement detected: printer %s supply %d (%s): %d%% → %d%%",
            printer.ip_address, supply.supply_index, supply.supply_color,
            previous_pct, current_pct,
        )
        # Determine event type
        event_type = "drum_replaced" if _is_drum(supply) else "toner_replaced"
        sent = _send(event_type, printer, supply, current_pct)
        _log_event(printer.id, event_type, supply, current_pct, sent, db_session)

        # Reset alert state for this lifecycle
        state.email_sent_warning = False
        state.email_sent_critical = False
        state.alert_level = "none"

    # Update last known level
    state.last_level_pct = current_pct

    if just_replaced:
        return

    # --- Threshold checks ---
    is_drum = _is_drum(supply)
    warn_thresh = cfg.drum_warning_pct if is_drum else cfg.toner_warning_pct
    crit_thresh = cfg.drum_critical_pct if is_drum else cfg.toner_critical_pct
    warn_type = "drum_warning" if is_drum else "toner_warning"
    crit_type = "drum_critical" if is_drum else "toner_critical"

    if current_pct <= crit_thresh and not state.email_sent_critical:
        sent = _send(crit_type, printer, supply, current_pct)
        _log_event(printer.id, crit_type, supply, current_pct, sent, db_session)
        state.email_sent_critical = True
        state.alert_level = "critical"

    elif current_pct <= warn_thresh and not state.email_sent_warning:
        sent = _send(warn_type, printer, supply, current_pct)
        _log_event(printer.id, warn_type, supply, current_pct, sent, db_session)
        state.email_sent_warning = True
        if state.alert_level == "none":
            state.alert_level = "warning"


def _evaluate_offline(printer: Printer, data: PrinterData, db_session: Session) -> None:
    """Evaluate offline / back-online status."""
    state = _get_or_create_state(printer.id, DEVICE_SUPPLY_INDEX, db_session)

    if not data.is_online:
        if (
            printer.consecutive_failures >= config.alerts.offline_after_failures
            and state.alert_level != "offline"
        ):
            sent = _send("printer_offline", printer, None, None)
            _log_event(printer.id, "printer_offline", None, None, sent, db_session)
            state.alert_level = "offline"
    else:
        if state.alert_level == "offline":
            # Printer came back online
            sent = _send("printer_online", printer, None, None)
            _log_event(printer.id, "printer_online", None, None, sent, db_session)
            state.alert_level = "none"
            state.email_sent_warning = False
            state.email_sent_critical = False


def _is_drum(supply: SupplyData) -> bool:
    return supply.supply_type in ("opc", "drumUnit")


def evaluate(printer: Printer, data: PrinterData, db_session: Session) -> None:
    """
    Main entry point for the alert evaluator.
    Called after every poll with the fresh PrinterData.
    """
    _evaluate_offline(printer, data, db_session)

    if not data.is_online:
        return

    for supply in data.supplies:
        try:
            _evaluate_supply(printer, supply, db_session)
        except Exception:
            logger.exception(
                "Supply evaluation error for printer %s supply %d",
                printer.ip_address, supply.supply_index,
            )
