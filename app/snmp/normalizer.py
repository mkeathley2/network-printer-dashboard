"""
Converts raw SNMP data into clean PrinterData / SupplyData dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# Mapping of prtMarkerSuppliesType integer → human-readable type string
SUPPLY_TYPE_MAP = {
    1: "other",
    2: "unknown",
    3: "tonerCartridge",
    4: "inkCartridge",
    5: "inkRibbon",
    6: "wasteToner",
    7: "opc",            # drum / imaging unit
    8: "developerOil",
    9: "wasteInk",
    10: "opc",
    11: "cleanerFluid",
    12: "fuseroil",
    13: "solidWax",
    14: "ribbonWax",
    15: "wasteWax",
    16: "ipm",
}

# Color name normalisation
COLOR_ALIASES = {
    "black":   "black",
    "k":       "black",
    "bk":      "black",
    "cyan":    "cyan",
    "c":       "cyan",
    "magenta": "magenta",
    "m":       "magenta",
    "yellow":  "yellow",
    "y":       "yellow",
}


def normalize_color(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    key = raw.strip().lower()
    return COLOR_ALIASES.get(key, key)


def normalize_supply_type(type_int: Optional[int]) -> str:
    if type_int is None:
        return "unknown"
    return SUPPLY_TYPE_MAP.get(type_int, f"type_{type_int}")


def compute_pct(level_current: Optional[int], level_max: Optional[int]) -> Optional[int]:
    """
    Compute 0-100 percentage.
    Special Printer-MIB values:
      -3 = capacity unknown
      -2 = no restriction (unlimited)
      -1 = unknown
    Returns None when indeterminate.
    """
    if level_current is None or level_max is None:
        return None
    if level_current < 0 or level_max <= 0:
        return None
    pct = round((level_current / level_max) * 100)
    return max(0, min(100, pct))


@dataclass
class SupplyData:
    supply_index: int
    supply_type: str = "unknown"
    supply_color: str = "unknown"
    description: str = ""
    level_current: Optional[int] = None
    level_max: Optional[int] = None
    level_pct: Optional[int] = None


@dataclass
class PrinterData:
    ip_address: str
    is_online: bool = False
    vendor: str = "generic"
    model: Optional[str] = None
    serial_number: Optional[str] = None
    sysname: Optional[str] = None
    uptime_seconds: Optional[int] = None
    page_count: Optional[int] = None
    status_raw: Optional[str] = None
    error_state_raw: Optional[str] = None
    supplies: List[SupplyData] = field(default_factory=list)
