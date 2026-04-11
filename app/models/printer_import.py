from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PrinterImportData(Base):
    """Staging table populated by spreadsheet upload. Checked when a printer is added."""

    __tablename__ = "printer_import_data"

    ip_address: Mapped[str] = mapped_column(String(45), primary_key=True)
    location_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    assigned_person: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    sql_number: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    assigned_computer: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone_ext: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    printer_web_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    printer_web_password: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<PrinterImportData {self.ip_address!r}>"
