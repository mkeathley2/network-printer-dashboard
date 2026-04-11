"""
SNMP OID registry.
All OIDs are string dotted-decimal notation, no leading dot.
"""

# ---------------------------------------------------------------------------
# Standard / Generic
# ---------------------------------------------------------------------------

# System MIB (RFC 1213)
SYSDESCR        = "1.3.6.1.2.1.1.1.0"
SYSOID          = "1.3.6.1.2.1.1.2.0"
SYSUPTIME       = "1.3.6.1.2.1.1.3.0"
SYSNAME         = "1.3.6.1.2.1.1.5.0"
SYSLOCATION     = "1.3.6.1.2.1.1.6.0"

# HOST-RESOURCES-MIB (RFC 2790)
HR_DEVICE_STATUS            = "1.3.6.1.2.1.25.3.2.1.5.1"
HR_DEVICE_DESCR             = "1.3.6.1.2.1.25.3.2.1.3.1"   # clean model name on most printers
HR_PRINTER_DETECTED_ERRORS  = "1.3.6.1.2.1.25.3.5.1.2.1"

# Printer-MIB (RFC 3805) — supply table base OIDs (use as WALK prefix)
PRT_MARKER_SUPPLIES_TABLE       = "1.3.6.1.2.1.43.11"
PRT_MARKER_SUPPLIES_TYPE        = "1.3.6.1.2.1.43.11.1.1.4.1"   # + .{index}
PRT_MARKER_SUPPLIES_DESC        = "1.3.6.1.2.1.43.11.1.1.6.1"   # + .{index}
PRT_MARKER_SUPPLIES_MAX_CAP     = "1.3.6.1.2.1.43.11.1.1.8.1"   # + .{index}
PRT_MARKER_SUPPLIES_LEVEL       = "1.3.6.1.2.1.43.11.1.1.9.1"   # + .{index}

# Printer-MIB — colorant table (color name for each supply)
PRT_MARKER_COLORANT_TABLE       = "1.3.6.1.2.1.43.12"
PRT_MARKER_COLORANT_VALUE       = "1.3.6.1.2.1.43.12.1.1.4.1"   # + .{index}

# Printer-MIB — page counter (lifetime impressions)
PRT_MARKER_LIFE_COUNT           = "1.3.6.1.2.1.43.10.2.1.4.1.1"

# Printer-MIB — serial number (prtGeneralSerialNumber, works on most RFC 3805 printers)
PRT_GENERAL_SERIAL_NUMBER       = "1.3.6.1.2.1.43.5.1.1.17.1"

# prtMarkerSuppliesType values of interest
SUPPLY_TYPE_TONER   = 3
SUPPLY_TYPE_INK     = 4
SUPPLY_TYPE_DRUM    = 7
SUPPLY_TYPE_WASTE   = 100   # not standard; many vendors use higher values

# ---------------------------------------------------------------------------
# Vendor sysObjectID prefixes for auto-detection
# ---------------------------------------------------------------------------
VENDOR_OID_PREFIXES = {
    "1.3.6.1.4.1.11.":   "hp",
    "1.3.6.1.4.1.2435.": "brother",
    "1.3.6.1.4.1.1602.": "canon",
    "1.3.6.1.4.1.1347.": "kyocera",
    "1.3.6.1.4.1.367.":  "ricoh",
}

# ---------------------------------------------------------------------------
# HP / HPE
# ---------------------------------------------------------------------------
HP_SYSOID_PREFIX    = "1.3.6.1.4.1.11"
HP_SERIAL_NUMBER    = "1.3.6.1.4.1.11.2.3.9.4.2.2.5.1.1.17"
HP_TOTAL_PAGES      = "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.5"
HP_DEVICE_STATUS    = "1.3.6.1.4.1.11.2.3.9.1.1.2.1.0"

# ---------------------------------------------------------------------------
# Brother
# ---------------------------------------------------------------------------
BROTHER_SYSOID_PREFIX   = "1.3.6.1.4.1.2435"
BROTHER_MODEL           = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0"
BROTHER_SERIAL          = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.17.0"
BROTHER_PAGE_COUNT      = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0"
BROTHER_TONER_BLACK     = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.10.0"
BROTHER_DRUM_BLACK      = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.55.0"

# ---------------------------------------------------------------------------
# Canon
# ---------------------------------------------------------------------------
CANON_SYSOID_PREFIX     = "1.3.6.1.4.1.1602"
CANON_PAGE_COUNT        = "1.3.6.1.4.1.1602.1.1.1.10.0"
CANON_SERIAL            = "1.3.6.1.4.1.1602.1.11.1.2.1.4.2"
CANON_MODEL             = "1.3.6.1.4.1.1602.1.1.1.1.0"

# ---------------------------------------------------------------------------
# Kyocera / ECOSYS
# ---------------------------------------------------------------------------
KYOCERA_SYSOID_PREFIX   = "1.3.6.1.4.1.1347"
KYOCERA_SERIAL          = "1.3.6.1.4.1.1347.43.5.1.1.28.1"
KYOCERA_PAGE_COUNT      = "1.3.6.1.4.1.1347.43.10.1.1.10.1.1"
KYOCERA_MODEL           = "1.3.6.1.4.1.1347.43.5.1.1.1.1"

# ---------------------------------------------------------------------------
# Ricoh / Aficio / Lanier / Savin / Nashuatec (enterprise 367)
# ---------------------------------------------------------------------------
RICOH_SYSOID_PREFIX     = "1.3.6.1.4.1.367"
RICOH_MODEL             = "1.3.6.1.4.1.367.3.2.1.2.19.2.0"
RICOH_SERIAL            = "1.3.6.1.4.1.367.3.2.1.2.19.52.0"
RICOH_PAGE_COUNT        = "1.3.6.1.4.1.367.3.2.1.2.24.1.0"
