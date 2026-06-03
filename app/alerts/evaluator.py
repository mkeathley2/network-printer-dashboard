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
            critical_ticket_sent=False,
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

        # Reset alert state for this lifecycle (fresh cartridge → fresh
        # critical-ticket budget, fresh predictive-alert budget, fresh email
        # dedup flags).
        state.email_sent_warning = False
        state.email_sent_critical = False
        state.critical_ticket_sent = False
        state.predictive_alert_sent = False
        state.alert_level = "none"

        # Auto-create a cost-entry helpdesk ticket (admin can toggle).
        # No dedup needed: each replacement is itself a unique event.
        _maybe_cost_entry_ticket(
            printer, supply,
            previous_pct=previous_pct,
            new_pct=current_pct,
            db_session=db_session,
        )

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

        # Auto-create a helpdesk ticket (once per supply lifecycle) if the
        # admin has enabled it.  This fires alongside the critical email so
        # the helpdesk team has an actionable record without anyone clicking
        # "Create Helpdesk Ticket" manually.
        _maybe_auto_ticket(printer, supply, current_pct, state, db_session)

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


def _maybe_auto_ticket(
    printer: Printer,
    supply: SupplyData,
    current_pct: int,
    state: AlertState,
    db_session: Session,
) -> None:
    """
    Auto-create a helpdesk ticket the first time this supply hits critical.

    Gated by the ``auto_ticket_on_critical_enabled`` SiteSetting.  Deduped via
    ``state.critical_ticket_sent`` — only fires once per supply lifecycle;
    resets when a replacement is detected.
    """
    if state.critical_ticket_sent:
        return
    try:
        from app.models import SiteSetting
        enabled_row = db_session.get(SiteSetting, "auto_ticket_on_critical_enabled")
        if not (enabled_row and enabled_row.value == "1"):
            return
    except Exception:
        logger.exception("Could not read auto_ticket_on_critical_enabled setting")
        return

    try:
        from app.alerts.notifier import send_helpdesk_ticket
        kind = "drum" if _is_drum(supply) else "toner"
        color = (supply.supply_color or "unknown").title()
        desc = supply.description or f"{color} {kind.title()}"
        note = (
            f"AUTOMATIC TICKET: {kind} critically low.\n\n"
            f"Supply:        {desc}\n"
            f"Current Level: {current_pct}%\n\n"
            f"Please order or replace this supply as soon as possible. "
            f"This ticket was auto-generated when the supply first crossed "
            f"the critical threshold; you will not receive another for this "
            f"cartridge until it is replaced."
        )
        # Latest supply rows for the ticket body — query directly so we don't
        # depend on the caller passing them.
        from app.models import SupplySnapshot, TelemetrySnapshot
        latest = (
            db_session.query(TelemetrySnapshot)
            .filter_by(printer_id=printer.id)
            .order_by(TelemetrySnapshot.polled_at.desc())
            .first()
        )
        supplies_for_ticket = []
        if latest:
            supplies_for_ticket = (
                db_session.query(SupplySnapshot)
                .filter_by(telemetry_id=latest.id)
                .order_by(SupplySnapshot.supply_index)
                .all()
            )

        ok, msg = send_helpdesk_ticket(
            printer, supplies_for_ticket, note, "system/auto-critical",
        )
        if ok:
            state.critical_ticket_sent = True
            logger.info(
                "Auto-helpdesk-ticket sent for printer %s supply %d (%s critical at %d%%)",
                printer.ip_address, supply.supply_index, kind, current_pct,
            )
        else:
            logger.warning(
                "Auto-helpdesk-ticket failed for printer %s: %s",
                printer.ip_address, msg,
            )
    except Exception:
        logger.exception(
            "Error firing auto-helpdesk-ticket for printer %s supply %d",
            printer.ip_address, supply.supply_index,
        )


def _maybe_cost_entry_ticket(
    printer: Printer,
    supply: SupplyData,
    previous_pct: int | None,
    new_pct: int | None,
    db_session: Session,
) -> None:
    """
    Auto-create a helpdesk ticket asking the tech to log the replacement cost.

    Gated by the ``auto_ticket_on_replacement_enabled`` SiteSetting.  Fires
    once per detected replacement (no dedup needed — each replacement is
    itself a unique one-shot event).
    """
    try:
        from app.models import SiteSetting
        row = db_session.get(SiteSetting, "auto_ticket_on_replacement_enabled")
        if not (row and row.value == "1"):
            return
    except Exception:
        logger.exception("Could not read auto_ticket_on_replacement_enabled setting")
        return

    try:
        from app.alerts.notifier import send_cost_entry_ticket
        kind_is_drum = _is_drum(supply)
        color = supply.supply_color or "unknown"
        desc = supply.description or supply.supply_type or "Supply"
        ok, msg = send_cost_entry_ticket(
            printer,
            supply_color=color,
            supply_description=desc,
            previous_level_pct=previous_pct,
            new_level_pct=new_pct,
            is_drum=kind_is_drum,
        )
        if ok:
            logger.info(
                "Cost-entry ticket sent for printer %s supply %d (%s replacement %s%% -> %s%%)",
                printer.ip_address, supply.supply_index, color,
                previous_pct, new_pct,
            )
        else:
            logger.warning(
                "Cost-entry ticket failed for printer %s: %s",
                printer.ip_address, msg,
            )
    except Exception:
        logger.exception(
            "Error firing cost-entry ticket for printer %s supply %d",
            printer.ip_address, supply.supply_index,
        )


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
