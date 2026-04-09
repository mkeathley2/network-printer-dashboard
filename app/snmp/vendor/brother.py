"""Brother-specific SNMP enrichment."""
from __future__ import annotations

import logging

from app.snmp import oids
from app.snmp.client import snmp_get
from app.snmp.normalizer import PrinterData

logger = logging.getLogger(__name__)


def enrich(data: PrinterData, snmp_params: dict, timeout: int = 3, retries: int = 2) -> None:
    brother_oids = [
        oids.BROTHER_MODEL,
        oids.BROTHER_SERIAL,
        oids.BROTHER_PAGE_COUNT,
    ]
    result = snmp_get(data.ip_address, brother_oids, snmp_params, timeout=timeout, retries=retries)

    if not result:
        return

    for k, v in result.items():
        k_stripped = k.lstrip(".")
        if k_stripped.startswith(oids.BROTHER_MODEL.lstrip(".")):
            if v and not data.model:
                data.model = str(v).strip()
        elif k_stripped.startswith(oids.BROTHER_SERIAL.lstrip(".")):
            if v and not data.serial_number:
                data.serial_number = str(v).strip()
        elif k_stripped.startswith(oids.BROTHER_PAGE_COUNT.lstrip(".")):
            if v is not None and data.page_count is None:
                try:
                    data.page_count = int(v)
                except (ValueError, TypeError):
                    pass
