"""Brother-specific SNMP enrichment.

Brother mono lasers (HL-L23xx, DCP/MFC-L2xxx, etc.) report the toner level in
the standard Printer-MIB with sentinel values (level=-3, maxCapacity=-2 —
"at least one supply unit remaining"), so no percentage can ever be computed
from the MIB. The drum reports real numbers, which is why drums work while
toner shows nothing.

The real toner percentage lives in Brother's proprietary maintenance blob at
1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0. Blob format (verified live against an
HL-L2350DW): consecutive 7-byte records ``[id][0x01][0x04][4-byte BE value]``
terminated by ``0xff``:

    id 0x81 = toner remaining %          (0-100)
    id 0x6f = toner remaining % x 100    (0-10000)
    id 0x41 = drum remaining  % x 100
    id 0x11 = page count

The decode was cross-verified: the drum record matches the standard MIB's
drum level exactly.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.snmp import oids
from app.snmp.client import snmp_get
from app.snmp.normalizer import PrinterData

logger = logging.getLogger(__name__)


def _blob_to_bytes(value) -> bytes:
    """
    The maintenance blob is binary, so _clean_octet_string() returns it as a
    pysnmp hex string ("0x6301…"). Convert back to bytes; tolerate raw bytes
    and (unlikely) printable-decoded strings.
    """
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    s = str(value).strip()
    if s.lower().startswith("0x"):
        try:
            return bytes.fromhex(s[2:])
        except ValueError:
            return b""
    return s.encode("latin-1", errors="ignore")


def _decode_maintenance_blob(value) -> dict[int, int]:
    """Parse the 7-byte records into {id: value}. Stops at 0xff or malformed data."""
    raw = _blob_to_bytes(value)
    records: dict[int, int] = {}
    i = 0
    while i + 7 <= len(raw):
        rid = raw[i]
        if rid == 0xFF:
            break
        # Expected layout: [id][0x01][0x04][4-byte big-endian value]
        if raw[i + 1] == 0x01 and raw[i + 2] == 0x04:
            records[rid] = int.from_bytes(raw[i + 3:i + 7], "big")
            i += 7
        else:
            break  # unknown layout — stop rather than misparse
    return records


def _toner_pct_from_records(records: dict[int, int]) -> Optional[int]:
    """Toner %: id 0x81 directly, falling back to 0x6f (which is % x 100)."""
    v = records.get(0x81)
    if v is not None and 0 <= v <= 100:
        return int(v)
    v = records.get(0x6F)
    if v is not None and 0 <= v <= 10000:
        return round(v / 100)
    return None


def enrich(data: PrinterData, snmp_params: dict, timeout: int = 3, retries: int = 2) -> None:
    brother_oids = [
        oids.BROTHER_SERIAL,
        oids.BROTHER_MAINTENANCE,
    ]
    result = snmp_get(data.ip_address, brother_oids, snmp_params, timeout=timeout, retries=retries)
    if not result:
        return

    serial_val = None
    maintenance_val = None
    for k, v in result.items():
        k_stripped = k.lstrip(".")
        if k_stripped.startswith(oids.BROTHER_SERIAL.lstrip(".")):
            serial_val = v
        elif k_stripped.startswith(oids.BROTHER_MAINTENANCE.lstrip(".")):
            maintenance_val = v

    # Serial — only fill the gap, never overwrite the generic probe's value
    if serial_val and not data.serial_number:
        s = str(serial_val).strip()
        if s:
            data.serial_number = s

    records = _decode_maintenance_blob(maintenance_val)

    # Page count fallback (blob id 0x11) when the generic probe got none
    if data.page_count is None and 0x11 in records:
        data.page_count = records[0x11]

    # Toner % — only when exactly ONE toner supply lacks a level (the mono
    # case). Color Brothers report standard MIB levels, and with multiple
    # unknown toners we can't know which color the blob value belongs to.
    unknown_toners = [
        s for s in data.supplies
        if s.supply_type == "tonerCartridge" and s.level_pct is None
    ]
    if len(unknown_toners) != 1:
        return

    pct = _toner_pct_from_records(records)
    if pct is not None:
        unknown_toners[0].level_pct = pct
        logger.debug(
            "Brother enrich: toner level %d%% from maintenance blob for %s",
            pct, data.ip_address,
        )
