"""Kyocera/ECOSYS-specific SNMP enrichment."""
from __future__ import annotations

import logging

from app.snmp import oids
from app.snmp.client import snmp_get
from app.snmp.normalizer import PrinterData

logger = logging.getLogger(__name__)


def enrich(data: PrinterData, snmp_params: dict, timeout: int = 3, retries: int = 2) -> None:
    kyocera_oids = [
        oids.KYOCERA_MODEL,
        oids.KYOCERA_SERIAL,
        oids.KYOCERA_PAGE_COUNT,
    ]
    result = snmp_get(data.ip_address, kyocera_oids, snmp_params, timeout=timeout, retries=retries)

    if not result:
        return

    for k, v in result.items():
        k_stripped = k.lstrip(".")
        if k_stripped.startswith(oids.KYOCERA_MODEL.lstrip(".")):
            if v:
                model = str(v).strip()
                # Always prefer the vendor-specific model over the generic sysDescr value
                if model:
                    data.model = model
        elif k_stripped.startswith(oids.KYOCERA_SERIAL.lstrip(".")):
            if v:
                serial = str(v).strip()
                # Sanity-check: real serial numbers are at least 5 chars.
                # Values of 1–3 chars are likely status codes, not serials.
                if len(serial) >= 5:
                    data.serial_number = serial
                elif not data.serial_number:
                    logger.debug(
                        "Kyocera serial OID returned short value %r for %s — "
                        "likely a status code, ignoring. "
                        "Will fall back to prtGeneralSerialNumber.",
                        serial, data.ip_address,
                    )
        elif k_stripped.startswith(oids.KYOCERA_PAGE_COUNT.lstrip(".")):
            if v is not None and data.page_count is None:
                try:
                    data.page_count = int(v)
                except (ValueError, TypeError):
                    pass
