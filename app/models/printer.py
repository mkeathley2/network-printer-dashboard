from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey,
    SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PrinterGroup(Base):
    __tablename__ = "printer_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    printers: Mapped[List["Printer"]] = relationship("Printer", back_populates="group")

    def __repr__(self) -> str:
        return f"<PrinterGroup {self.name!r}>"


class Printer(Base):
    __tablename__ = "printers"
    __table_args__ = (
        # Composite unique so two agents at different sites can share the same IP.
        # In MariaDB/MySQL, NULL values are distinct in unique indexes, so
        # (192.168.1.5, NULL) (local) and (192.168.1.5, 1) (agent 1) coexist fine.
        UniqueConstraint("ip_address", "agent_id", name="uq_printer_ip_agent"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    hostname: Mapped[Optional[str]] = mapped_column(String(255))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    vendor: Mapped[str] = mapped_column(
        Enum("hp", "brother", "canon", "kyocera", "ricoh", "generic", name="vendor_enum"),
        nullable=False,
        default="generic",
    )
    model: Mapped[Optional[str]] = mapped_column(String(255))
    serial_number: Mapped[Optional[str]] = mapped_column(String(128))
    mac_address: Mapped[Optional[str]] = mapped_column(String(17))

    # SNMP credentials
    snmp_version: Mapped[str] = mapped_column(
        Enum("2c", "3", "1", name="snmp_version_enum"),
        nullable=False,
        default="2c",
    )
    snmp_community: Mapped[str] = mapped_column(String(128), nullable=False, default="public")
    # SNMPv3 fields (NULL when using v2c)
    snmp_v3_user: Mapped[Optional[str]] = mapped_column(String(64))
    snmp_v3_auth_proto: Mapped[Optional[str]] = mapped_column(Enum("MD5", "SHA", name="snmp_auth_proto_enum"))
    snmp_v3_auth_key: Mapped[Optional[str]] = mapped_column(String(512))   # Fernet-encrypted
    snmp_v3_priv_proto: Mapped[Optional[str]] = mapped_column(Enum("DES", "AES", name="snmp_priv_proto_enum"))
    snmp_v3_priv_key: Mapped[Optional[str]] = mapped_column(String(512))   # Fernet-encrypted

    # Remote agent (NULL = locally monitored printer)
    agent_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("remote_agents.id", ondelete="SET NULL"), nullable=True
    )
    agent: Mapped[Optional["RemoteAgent"]] = relationship(  # type: ignore[name-defined]
        "RemoteAgent", back_populates="printers", foreign_keys=[agent_id]
    )

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consecutive_failures: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    # Counts how many consecutive agent checkins this printer was absent from.
    # Used only for remote-agent printers; local printers use consecutive_failures.
    consecutive_misses: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("printer_groups.id", ondelete="SET NULL"))
    group: Mapped[Optional[PrinterGroup]] = relationship("PrinterGroup", back_populates="printers")

    # Location (replaces group in UI)
    location_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    location: Mapped[Optional["Location"]] = relationship("Location", back_populates="printers")  # type: ignore[name-defined]

    # Asset / assignment fields (populated from spreadsheet import or manual entry)
    assigned_person: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    sql_number: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    assigned_computer: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone_ext: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    printer_web_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    printer_web_password: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Per-printer alert thresholds (NULL = use site-wide default from SiteSetting)
    supply_warn_pct: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    supply_crit_pct: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)

    telemetry: Mapped[List["TelemetrySnapshot"]] = relationship(  # type: ignore[name-defined]
        "TelemetrySnapshot", back_populates="printer", cascade="all, delete-orphan"
    )
    alert_events: Mapped[List["AlertEvent"]] = relationship(  # type: ignore[name-defined]
        "AlertEvent", back_populates="printer", cascade="all, delete-orphan"
    )
    alert_states: Mapped[List["AlertState"]] = relationship(  # type: ignore[name-defined]
        "AlertState", back_populates="printer", cascade="all, delete-orphan"
    )

    @property
    def effective_name(self) -> str:
        return self.sql_number or self.display_name or self.hostname or self.ip_address

    def __repr__(self) -> str:
        return f"<Printer {self.ip_address} {self.model!r}>"
