"""
Generic Printer-MIB (RFC 3805) + HOST-RESOURCES-MIB (RFC 2790) probe.
Works for any SNMP-capable printer regardless of vendor.
After the generic probe, vendor-specific modules may enrich the result.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.snmp import oids
from app.snmp.client import snmp_get, snmp_walk
from app.snmp.normalizer import (
    PrinterData, SupplyData,
    compute_pct, normalize_color, normalize_supply_type,
)

logger = logging.getLogger(__name__)


def _detect_vendor(sysoid_value: Optional[str], sysdescr: Optional[str]) -> str:
    """Detect vendor from sysObjectID prefix, with sysDescr fallback."""
    if sysoid_value:
        for prefix, vendor in oids.VENDOR_OID_PREFIXES.items():
            if sysoid_value.startswith(prefix):
                return vendor
    # Fallback: check sysDescr string
    if sysdescr:
        descr_lower = sysdescr.lower()
        if "hp" in descr_lower or "hewlett" in descr_lower or "laserjet" in descr_lower:
            return "hp"
        if "brother" in descr_lower:
            return "brother"
        if "canon" in descr_lower:
            return "canon"
        if "kyocera" in descr_lower or "ecosys" in descr_lower:
            return "kyocera"
    return "generic"


def probe(ip: str, snmp_params: dict, timeout: int = 3, retries: int = 2) -> PrinterData:
    """
    Perform a full generic probe of a printer.
    Returns a PrinterData instance (is_online=False on complete failure).
    """
    data = PrinterData(ip_address=ip)

    # --- Basic system info ---
    sys_oids = [
        oids.SYSDESCR,
        oids.SYSOID,
        oids.SYSUPTIME,
        oids.SYSNAME,
        oids.HR_DEVICE_STATUS,
        oids.HR_PRINTER_DETECTED_ERRORS,
        oids.PRT_MARKER_LIFE_COUNT,
        oids.PRT_GENERAL_SERIAL_NUMBER,
    ]
    sys_result = snmp_get(ip, sys_oids, snmp_params, timeout=timeout, retries=retries)

    if not sys_result:
        # No response at all → offline
        return data

    data.is_online = True

    sysdescr_val = _first_val(sys_result, oids.SYSDESCR)
    sysoid_val   = _first_val(sys_result, oids.SYSOID)
    sysname_val  = _first_val(sys_result, oids.SYSNAME)
    uptime_val   = _first_val(sys_result, oids.SYSUPTIME)
    status_val   = _first_val(sys_result, oids.HR_DEVICE_STATUS)
    error_val    = _first_val(sys_result, oids.HR_PRINTER_DETECTED_ERRORS)
    page_val     = _first_val(sys_result, oids.PRT_MARKER_LIFE_COUNT)

    serial_val   = _first_val(sys_result, oids.PRT_GENERAL_SERIAL_NUMBER)

    data.vendor = _detect_vendor(sysoid_val, sysdescr_val)
    data.sysname = sysname_val
    data.model = _extract_model_from_descr(sysdescr_val)
    if serial_val:
        data.serial_number = str(serial_val).strip() or None
    data.status_raw = str(status_val) if status_val is not None else None
    data.error_state_raw = str(error_val) if error_val is not None else None

    if uptime_val is not None:
        try:
            # TimeTicks are in hundredths of a second
            data.uptime_seconds = int(uptime_val) // 100
        except (ValueError, TypeError):
            pass

    if page_val is not None:
        try:
            data.page_count = int(page_val)
        except (ValueError, TypeError):
            pass

    # --- Supply table walk ---
    supply_walk = snmp_walk(
        ip, oids.PRT_MARKER_SUPPLIES_TABLE, snmp_params, timeout=timeout, retries=retries
    )
    data.supplies = _parse_supply_walk(supply_walk)

    # If supplies is empty, try walking colorant table for color names
    if data.supplies:
        _enrich_colors_from_walk(ip, snmp_params, data, timeout, retries)

    return data


def _first_val(result: dict, oid_prefix: str):
    """Return first value whose OID key starts with oid_prefix (strips leading dot)."""
    prefix = oid_prefix.lstrip(".")
    for k, v in result.items():
        if k.lstrip(".").startswith(prefix):
            return v
    return None


def _extract_model_from_descr(sysdescr: Optional[str]) -> Optional[str]:
    """Try to extract a model string from sysDescr."""
    if not sysdescr:
        return None
    # HP format: "HP ETHERNET MULTI-ENVIRONMENT,...,PID:HP LaserJet M402dn"
    if "PID:" in sysdescr:
        return sysdescr.split("PID:")[-1].strip()[:255]
    # Many printers put model in first line of sysDescr
    first_line = sysdescr.strip().split("\n")[0].strip()
    return first_line[:255] if first_line else None


def _parse_supply_walk(walk_rows: list) -> list[SupplyData]:
    """
    Parse raw walk rows from prtMarkerSuppliesTable into SupplyData objects.
    The table has sub-OIDs like: .43.11.1.1.{col}.1.{index}
    col 4 = type, col 6 = description, col 8 = maxCapacity, col 9 = level
    """
    # Collect by index
    supplies: dict[int, dict] = {}

    for oid_str, value in walk_rows:
        parts = oid_str.rstrip(".").split(".")
        # We expect OIDs ending in .{col}.1.{index}
        if len(parts) < 3:
            continue
        try:
            index = int(parts[-1])
            col = int(parts[-3])
        except (ValueError, IndexError):
            continue

        if index not in supplies:
            supplies[index] = {}

        if col == 4:
            supplies[index]["type_int"] = int(value) if value is not None else None
        elif col == 6:
            supplies[index]["description"] = str(value) if value else ""
        elif col == 8:
            supplies[index]["max_cap"] = int(value) if value is not None else None
        elif col == 9:
            supplies[index]["level"] = int(value) if value is not None else None

    result = []
    for idx, info in sorted(supplies.items()):
        type_int = info.get("type_int")
        level    = info.get("level")
        max_cap  = info.get("max_cap")
        desc     = info.get("description", "")

        # Try to infer color from description
        color = normalize_color(_color_from_desc(desc))

        sd = SupplyData(
            supply_index=idx,
            supply_type=normalize_supply_type(type_int),
            supply_color=color,
            description=desc,
            level_current=level,
            level_max=max_cap,
            level_pct=compute_pct(level, max_cap),
        )
        result.append(sd)

    return result


def _color_from_desc(desc: str) -> Optional[str]:
    """Infer color name from supply description string."""
    if not desc:
        return None
    low = desc.lower()
    for color in ("black", "cyan", "magenta", "yellow"):
        if color in low:
            return color
    if " k " in low or low.endswith(" k") or low.startswith("k "):
        return "black"
    return None


def _enrich_colors_from_walk(ip, snmp_params, data: PrinterData, timeout, retries) -> None:
    """Walk the colorant table to get explicit color names if available."""
    colorant_walk = snmp_walk(
        ip, oids.PRT_MARKER_COLORANT_TABLE, snmp_params, timeout=timeout, retries=retries
    )
    colorant_map: dict[int, str] = {}
    for oid_str, value in colorant_walk:
        parts = oid_str.rstrip(".").split(".")
        if len(parts) < 1 or not value:
            continue
        try:
            idx = int(parts[-1])
            colorant_map[idx] = normalize_color(str(value))
        except (ValueError, IndexError):
            continue

    # Assign colors where we got a match and current color is unknown
    for supply in data.supplies:
        if supply.supply_color == "unknown" and supply.supply_index in colorant_map:
            supply.supply_color = colorant_map[supply.supply_index]
