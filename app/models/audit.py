"""AuditLog model — records all user-initiated changes and system events."""
from __future__ import annotations

from datetime import datetime

from app.core.database import db


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id          = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    occurred_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    username    = db.Column(db.String(128), nullable=False)
    action      = db.Column(db.String(64),  nullable=False, index=True)
    target      = db.Column(db.String(255), nullable=True)
    detail      = db.Column(db.Text,        nullable=True)
    success     = db.Column(db.Boolean,     nullable=False, default=True)
