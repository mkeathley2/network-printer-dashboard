"""Audit logging helper.

Call `audit(username, action, target, detail, success)` from any route.
Each call opens its own short-lived DB session so the audit record always
persists even if the main request transaction is rolled back.

Entries older than RETENTION_DAYS are pruned on each write.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30


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
