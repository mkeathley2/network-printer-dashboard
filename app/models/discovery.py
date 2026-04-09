from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger, DateTime, Enum, ForeignKey,
    Integer, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class DiscoveryScan(Base):
    """Tracks a single discovery run (CIDR sweep or manual add)."""
    __tablename__ = "discovery_scans"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scan_type: Mapped[str] = mapped_column(
        Enum("cidr", "manual", name="scan_type_enum"), nullable=False
    )
    cidr_range: Mapped[Optional[str]] = mapped_column(String(50))
    manual_ip: Mapped[Optional[str]] = mapped_column(String(45))
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    hosts_probed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hosts_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        Enum("running", "complete", "failed", name="scan_status_enum"),
        nullable=False,
        default="running",
    )

    results: Mapped[List["DiscoveryResult"]] = relationship(
        "DiscoveryResult", back_populates="scan", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<DiscoveryScan {self.scan_type} {self.cidr_range or self.manual_ip} {self.status}>"


class DiscoveryResult(Base):
    """One row per IP probed during a discovery scan."""
    __tablename__ = "discovery_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("discovery_scans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    hostname: Mapped[Optional[str]] = mapped_column(String(255))
    vendor_detected: Mapped[Optional[str]] = mapped_column(String(64))
    model_detected: Mapped[Optional[str]] = mapped_column(String(255))
    snmp_responsive: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    already_known: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    printer_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("printers.id", ondelete="SET NULL")
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    scan: Mapped[DiscoveryScan] = relationship("DiscoveryScan", back_populates="results")

    def __repr__(self) -> str:
        return f"<DiscoveryResult {self.ip_address} responsive={self.snmp_responsive}>"
