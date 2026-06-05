"""Audit logging helper.

Call `audit(username, action, target, detail, success)` from any route.
Each call opens its own short-lived DB session so the audit record always
persists even if the main request transaction is rolled back.

This is the ADMIN AUDIT TRAIL only (logins, setting changes, printer
adds/edits, etc.). It is NOT where monitoring data lives — toner/drum
replacement history, costs, alert events, page counts and supply levels are
stored in separate tables (AlertEvent / SupplySnapshot / TelemetrySnapshot)
and are kept permanently. Only this audit trail is auto-pruned: entries
older than RETENTION_DAYS are deleted on each write.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

RETENTION_DAYS = 365


def audit(
    username: str,
    action: str,
    target: str | None = None,
    detail: str | None = None,
    success: bool = True,
) -> None:
    """Write one audit record and prune old entries.

    Safe to call from any thread or request context — uses a dedicated
    session via get_db() so it never interferes with the caller's transaction.
    """
    try:
        from app.core.database import get_db
        from app.models.audit import AuditLog

        cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
        with get_db() as sess:
            sess.add(AuditLog(
                occurred_at=datetime.utcnow(),
                username=username,
                action=action,
                target=target,
                detail=detail,
                success=success,
            ))
            # Prune entries outside retention window
            sess.query(AuditLog).filter(AuditLog.occurred_at < cutoff).delete()
    except Exception:
        logger.exception("audit() failed — record not saved")
