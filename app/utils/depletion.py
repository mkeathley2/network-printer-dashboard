"""
Supply depletion estimation and page-volume averages.

Both the printer history page and the Consumption Rate report call into
``compute_supply_depletion`` so they get identical, replacement-aware results.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.models import SupplySnapshot, TelemetrySnapshot
from app.models.alert import AlertEvent
from app.utils.regression import linear_regression


def compute_supply_depletion(
    printer_id: int,
    supply_index: int,
    db_session,
    window_days: Optional[int] = 90,
) -> Optional[dict]:
    """
    Estimate supply depletion for a single printer/supply slot.

    Algorithm:
      1. Find the most recent toner_replaced / drum_replaced AlertEvent for this
         printer + supply_index.  If found, the regression starts from that event.
      2. Otherwise fall back to *now - window_days* (or all-time if window_days is None).
      3. Pull all SupplySnapshot rows after the cutoff, sorted by polled_at.
      4. Need at least 3 datapoints to make a reasonable estimate.
      5. Compute slope (% per day) via least squares.
      6. If slope < 0 (depleting), days_remaining = current_pct / |slope|.

    Returns a dict, or None if there is too little data to estimate.

    Returned dict::

        {
          'current_pct':         23.0,          # latest level
          'slope_pct_per_day':  -1.42,          # negative = depleting
          'days_remaining':     16.2,           # None when not depleting
          'predicted_empty_at': datetime,       # None when not depleting
          'data_points':        47,
          'cutoff_date':        datetime,       # start of the regression window
          'cutoff_reason':      'replacement' | 'window' | 'all',
        }
    """
    now = datetime.utcnow()

    # 1. Find most recent replacement event for this slot
    last_replacement = (
        db_session.query(AlertEvent)
        .filter(
            AlertEvent.printer_id == printer_id,
            AlertEvent.supply_index == supply_index,
            AlertEvent.event_type.in_(("toner_replaced", "drum_replaced")),
        )
        .order_by(AlertEvent.occurred_at.desc())
        .first()
    )

    if last_replacement:
        cutoff = last_replacement.occurred_at
        cutoff_reason = "replacement"
    elif window_days is None:
        cutoff = None
        cutoff_reason = "all"
    else:
        cutoff = now - timedelta(days=window_days)
        cutoff_reason = "window"

    # 2. Pull snapshots after the cutoff
    q = db_session.query(SupplySnapshot).filter(
        SupplySnapshot.printer_id == printer_id,
        SupplySnapshot.supply_index == supply_index,
        SupplySnapshot.level_pct.isnot(None),
    )
    if cutoff is not None:
        q = q.filter(SupplySnapshot.polled_at >= cutoff)
    readings = q.order_by(SupplySnapshot.polled_at.asc()).all()

    if len(readings) < 3:
        return None

    # 3. Linear regression on (days-since-cutoff, level_pct)
    epoch = readings[0].polled_at.timestamp()
    xs = [(r.polled_at.timestamp() - epoch) / 86400.0 for r in readings]
    ys = [float(r.level_pct) for r in readings]
    slope = linear_regression(xs, ys)  # % per day; negative = depleting

    current_pct = ys[-1]

    if slope < 0:
        days_remaining = current_pct / abs(slope)
        predicted_empty_at = now + timedelta(days=days_remaining)
    else:
        days_remaining = None
        predicted_empty_at = None

    return {
        "current_pct": current_pct,
        "slope_pct_per_day": slope,
        "days_remaining": days_remaining,
        "predicted_empty_at": predicted_empty_at,
        "data_points": len(readings),
        "cutoff_date": readings[0].polled_at,
        "cutoff_reason": cutoff_reason,
    }


def compute_pages_per_day(
    printer_id: int,
    db_session,
    window_days: Optional[int] = 90,
) -> Optional[float]:
    """
    Average pages printed per day over the window.  Computed as
    (latest_page_count - earliest_page_count) / span_in_days using the
    earliest and latest TelemetrySnapshot inside the window.

    Returns None when there isn't enough data (need ≥2 readings spanning
    at least a few hours).
    """
    now = datetime.utcnow()
    q = db_session.query(TelemetrySnapshot).filter(
        TelemetrySnapshot.printer_id == printer_id,
        TelemetrySnapshot.page_count.isnot(None),
    )
    if window_days is not None:
        q = q.filter(TelemetrySnapshot.polled_at >= now - timedelta(days=window_days))

    earliest = q.order_by(TelemetrySnapshot.polled_at.asc()).first()
    latest = q.order_by(TelemetrySnapshot.polled_at.desc()).first()

    if not earliest or not latest or earliest.id == latest.id:
        return None

    span_seconds = (latest.polled_at - earliest.polled_at).total_seconds()
    if span_seconds < 3600:  # need at least an hour of separation
        return None

    pages_delta = (latest.page_count or 0) - (earliest.page_count or 0)
    if pages_delta < 0:
        # Counter rolled over or printer was reset — can't average meaningfully
        return None

    span_days = span_seconds / 86400.0
    return pages_delta / span_days
