"""HP/HPE-specific SNMP enrichment."""
from __future__ import annotations

import logging

from app.snmp import oids
from app.snmp.client import snmp_get
from app.snmp.normalizer import PrinterData

logger = logging.getLogger(__name__)


def enrich(data: PrinterData, snmp_params: dict, timeout: int = 3, retries: int = 2) -> None:
    """Supplement generic PrinterData with HP-specific OID values."""
    hp_oids = [oids.HP_SERIAL_NUMBER, oids.HP_TOTAL_PAGES]
    result = snmp_get(data.ip_address, hp_oids, snmp_params, timeout=timeout, retries=retries)

    if not result:
        return

    for k, v in result.items():
        if k.lstrip(".").startswith(oids.HP_SERIAL_NUMBER.lstrip(".")):
            if v and not data.serial_number:
                data.serial_number = str(v).strip()
        elif k.lstrip(".").startswith(oids.HP_TOTAL_PAGES.lstrip(".")):
            if v is not None and data.page_count is None:
                try:
                    data.page_count = int(v)
                except (ValueError, TypeError):
                    pass
