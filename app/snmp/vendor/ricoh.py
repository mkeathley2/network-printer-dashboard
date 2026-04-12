"""Ricoh/Aficio/Lanier/Savin-specific SNMP enrichment."""
from __future__ import annotations

import logging

from app.snmp import oids
from app.snmp.client import snmp_get
from app.snmp.normalizer import PrinterData

logger = logging.getLogger(__name__)


def enrich(data: PrinterData, snmp_params: dict, timeout: int = 3, retries: int = 2) -> None:
    ricoh_oids = [
        oids.RICOH_MODEL,
        oids.RICOH_SERIAL,
        oids.RICOH_PAGE_COUNT,
    ]
    result = snmp_get(data.ip_address, ricoh_oids, snmp_params, timeout=timeout, retries=retries)

    if not result:
        return

    for k, v in result.items():
        if v is None:
            continue
        k_stripped = k.lstrip(".")
        if k_stripped.startswith(oids.RICOH_MODEL.lstrip(".")):
            model = str(v).strip()
            # Only use Ricoh-specific model if generic probe didn't already get one,
            # and reject purely numeric values (product ID codes, not model names)
            if model and not data.model and not model.isdigit():
                data.model = model
        elif k_stripped.startswith(oids.RICOH_SERIAL.lstrip(".")):
            serial = str(v).strip()
            if serial:
                data.serial_number = serial
        elif k_stripped.startswith(oids.RICOH_PAGE_COUNT.lstrip(".")):
            try:
                data.page_count = int(v)
            except (ValueError, TypeError):
                pass
