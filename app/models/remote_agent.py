from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RemoteAgent(Base):
    __tablename__ = "remote_agents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    location_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )
    location: Mapped[Optional["Location"]] = relationship("Location")  # type: ignore[name-defined]

    # SHA-256 hex digest of the plaintext API key.
    # The key itself is a cryptographically random 32-byte token (secrets.token_urlsafe(32)),
    # so SHA-256 provides equivalent security to bcrypt for this use case and allows O(1) lookup.
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    agent_version: Mapped[Optional[str]] = mapped_column(String(32))
    last_checkin_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_ip: Mapped[Optional[str]] = mapped_column(String(64))
    subnet: Mapped[Optional[str]] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        Enum("active", "inactive", "stale", "error", name="agent_status_enum"),
        nullable=False,
        default="active",
    )

    # Command pending delivery on next checkin.
    # Only one command at a time — new command overwrites previous.
    pending_command: Mapped[Optional[str]] = mapped_column(String(32))
    # JSON-encoded config payload for the "config" command
    pending_command_config: Mapped[Optional[str]] = mapped_column(Text)

    scan_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # Dedup flag: True once a "stale" alert email has been sent.
    # Reset to False when agent checks in again.
    stale_alert_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # JSON list of the most recent per-IP scan errors reported by the agent
    last_errors: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    printers: Mapped[List["Printer"]] = relationship(  # type: ignore[name-defined]
        "Printer",
        back_populates="agent",
        foreign_keys="[Printer.agent_id]",
    )

    @property
    def parsed_errors(self) -> list:
        """Return last_errors as a Python list (empty list if none)."""
        if not self.last_errors:
            return []
        try:
            return json.loads(self.last_errors)
        except Exception:
            return []

    def __repr__(self) -> str:
        return f"<RemoteAgent {self.name!r} status={self.status!r}>"
