from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey,
    SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class AlertEvent(Base):
    """Immutable log of every alert lifecycle event."""
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    printer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("printers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(
        Enum(
            "toner_warning",
            "toner_critical",
            "toner_replaced",
            "drum_warning",
            "drum_critical",
            "drum_replaced",
            "printer_offline",
            "printer_online",
            "discovery_new",
            name="alert_event_type_enum",
        ),
        nullable=False,
    )
    supply_index: Mapped[Optional[int]] = mapped_column(SmallInteger)   # NULL for device-level events
    supply_color: Mapped[Optional[str]] = mapped_column(String(32))
    level_pct_at_event: Mapped[Optional[int]] = mapped_column(SmallInteger)
    email_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    printer: Mapped["Printer"] = relationship("Printer", back_populates="alert_events")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<AlertEvent {self.event_type} printer={self.printer_id}>"


class AlertState(Base):
    """
    One row per printer per supply slot.
    Tracks whether alert emails have been sent in the current lifecycle.
    Reset when a replacement is detected.
    supply_index = -1 is used for device-level (offline) alerts.
    """
    __tablename__ = "alert_state"
    __table_args__ = (UniqueConstraint("printer_id", "supply_index", name="uq_printer_supply"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    printer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("printers.id", ondelete="CASCADE"), nullable=False
    )
    supply_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    alert_level: Mapped[str] = mapped_column(
        Enum("none", "warning", "critical", "offline", name="alert_level_enum"),
        nullable=False,
        default="none",
    )
    email_sent_warning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_sent_critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_level_pct: Mapped[Optional[int]] = mapped_column(SmallInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    printer: Mapped["Printer"] = relationship("Printer", back_populates="alert_states")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<AlertState printer={self.printer_id} supply={self.supply_index} level={self.alert_level}>"
