from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey,
    Integer, SmallInteger, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TelemetrySnapshot(Base):
    """One row per printer per polling cycle — device-level counters."""
    __tablename__ = "telemetry_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    printer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("printers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    polled_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False, index=True)
    is_online: Mapped[bool] = mapped_column(Boolean, nullable=False)
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    drum_pct: Mapped[Optional[int]] = mapped_column(SmallInteger)   # 0-100
    uptime_seconds: Mapped[Optional[int]] = mapped_column(BigInteger)
    status_raw: Mapped[Optional[str]] = mapped_column(String(64))
    error_state_raw: Mapped[Optional[str]] = mapped_column(String(255))

    printer: Mapped["Printer"] = relationship("Printer", back_populates="telemetry")  # type: ignore[name-defined]
    supplies: Mapped[List["SupplySnapshot"]] = relationship(
        "SupplySnapshot", back_populates="telemetry", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<TelemetrySnapshot printer={self.printer_id} at={self.polled_at}>"


class SupplySnapshot(Base):
    """One row per supply unit per polling cycle (supports CMYK + drum)."""
    __tablename__ = "supply_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telemetry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("telemetry_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized for direct chart queries without joins
    printer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    polled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    supply_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    supply_type: Mapped[Optional[str]] = mapped_column(String(64))      # e.g. 'tonerCartridge', 'drumUnit'
    supply_color: Mapped[Optional[str]] = mapped_column(String(32))     # 'black', 'cyan', 'magenta', 'yellow'
    supply_description: Mapped[Optional[str]] = mapped_column(String(255))
    level_current: Mapped[Optional[int]] = mapped_column(Integer)       # raw prtMarkerSuppliesLevel
    level_max: Mapped[Optional[int]] = mapped_column(Integer)           # raw prtMarkerSuppliesMaxCapacity
    level_pct: Mapped[Optional[int]] = mapped_column(SmallInteger)      # computed 0-100, NULL if indeterminate

    telemetry: Mapped[TelemetrySnapshot] = relationship("TelemetrySnapshot", back_populates="supplies")

    def __repr__(self) -> str:
        return f"<SupplySnapshot printer={self.printer_id} idx={self.supply_index} pct={self.level_pct}>"
