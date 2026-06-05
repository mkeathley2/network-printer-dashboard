"""
Low-level SNMP GET and WALK wrappers using pysnmp 7.x asyncio API.
Returns plain Python dicts/lists. All errors caught; callers get empty results on failure.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from pysnmp.hlapi.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    get_cmd,
    next_cmd,
    walk_cmd,
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
    usmDESPrivProtocol,
    usmAesCfb128Protocol,
    usmNoAuthProtocol,
    usmNoPrivProtocol,
)

logger = logging.getLogger(__name__)


def _build_auth(snmp_params: dict) -> CommunityData | UsmUserData:
    version = snmp_params.get("version", "2c")
    if version == "3":
        auth_proto_map = {
            "MD5": usmHMACMD5AuthProtocol,
            "SHA": usmHMACSHAAuthProtocol,
        }
        priv_proto_map = {
            "DES": usmDESPrivProtocol,
            "AES": usmAesCfb128Protocol,
        }
        return UsmUserData(
            userName=snmp_params.get("user", ""),
            authKey=snmp_params.get("auth_key") or None,
            privKey=snmp_params.get("priv_key") or None,
            authProtocol=auth_proto_map.get(snmp_params.get("auth_proto", ""), usmNoAuthProtocol),
            privProtocol=priv_proto_map.get(snmp_params.get("priv_proto", ""), usmNoPrivProtocol),
        )
    mp_model = 0 if version == "1" else 1  # 0=SNMPv1, 1=SNMPv2c
    return CommunityData(snmp_params.get("community", "public"), mpModel=mp_model)


async def _async_get_all(
    ip: str,
    oids: List[str],
    snmp_params: dict,
    timeout: int,
    retries: int,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    engine = SnmpEngine()
    auth = _build_auth(snmp_params)
    transport = await UdpTransportTarget.create((ip, 161), timeout=timeout, retries=retries)

    for oid_str in oids:
        try:
            error_indication, error_status, error_index, var_binds = await get_cmd(
                engine,
                auth,
                transport,
                ContextData(),
                ObjectType(ObjectIdentity(oid_str)),
            )
            if error_indication:
                break  # Host unreachable / timeout — stop trying remaining OIDs
            if error_status:
                continue  # This specific OID not supported — try the next one
            for var_bind in var_binds:
                result[str(var_bind[0])] = _coerce_value(var_bind[1])
        except Exception as e:
            logger.debug("snmp_get error for %s OID %s: %s", ip, oid_str, e)

    return result


async def _async_walk(
    ip: str,
    base_oid: str,
    snmp_params: dict,
    timeout: int,
    retries: int,
) -> List[Tuple[str, Any]]:
    result: List[Tuple[str, Any]] = []
    engine = SnmpEngine()
    auth = _build_auth(snmp_params)
    transport = await UdpTransportTarget.create((ip, 161), timeout=timeout, retries=retries)

    try:
        async for error_indication, error_status, error_index, var_binds in walk_cmd(
            engine,
            auth,
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if error_indication or error_status:
                break
            for var_bind in var_binds:
                result.append((str(var_bind[0]), _coerce_value(var_bind[1])))
    except Exception as e:
        logger.debug("snmp_walk error for %s OID %s: %s", ip, base_oid, e)

    return result


def snmp_get(
    ip: str,
    oids: List[str],
    snmp_params: Optional[dict] = None,
    timeout: int = 3,
    retries: int = 2,
) -> Dict[str, Any]:
    if snmp_params is None:
        snmp_params = {"version": "2c", "community": "public"}
    try:
        return asyncio.run(_async_get_all(ip, oids, snmp_params, timeout, retries))
    except Exception as e:
        logger.debug("snmp_get failed for %s: %s", ip, e)
        return {}


def snmp_walk(
    ip: str,
    base_oid: str,
    snmp_params: Optional[dict] = None,
    timeout: int = 3,
    retries: int = 2,
) -> List[Tuple[str, Any]]:
    if snmp_params is None:
        snmp_params = {"version": "2c", "community": "public"}
    try:
        return asyncio.run(_async_walk(ip, base_oid, snmp_params, timeout, retries))
    except Exception as e:
        logger.debug("snmp_walk failed for %s: %s", ip, e)
        return []


def _clean_octet_string(val: Any) -> str:
    """
    Convert a pysnmp OctetString to clean text.

    pysnmp's ``prettyPrint()`` returns a ``0x…`` hex string whenever the octets
    contain ANY non-printable byte — including the trailing NUL byte that HP
    (and other vendors) append to ``prtMarkerSuppliesDescription`` and
    ``prtMarkerColorantValue`` strings.  That hex leaks all the way to the UI
    (e.g. a supply description showing
    ``0x426c61636b20436172747269646765…00`` instead of "Black Cartridge HP 87X").

    We grab the raw bytes, strip trailing NULs + whitespace, and decode as
    text.  Genuinely-binary values (no printable decode) fall back to pysnmp's
    hex representation so we don't mangle MAC addresses, etc.
    """
    # Grab raw bytes (pysnmp OctetString supports .asOctets(); bytes() is a fallback)
    try:
        raw = val.asOctets()
    except Exception:
        try:
            raw = bytes(val)
        except Exception:
            try:
                return val.prettyPrint()
            except Exception:
                return str(val)

    stripped = raw.rstrip(b"\x00").rstrip()  # trailing NULs + whitespace
    if not stripped:
        return ""

    # All-printable ASCII (plus tab / newline / CR)? → decode as text.
    if all(0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D) for b in stripped):
        return stripped.decode("ascii", errors="replace").strip()

    # Try UTF-8 for accented characters; reject if control chars remain
    # (that would indicate genuinely binary data).
    try:
        text = stripped.decode("utf-8")
        if all(ord(c) >= 0x20 or c in "\t\n\r" for c in text):
            return text.strip()
    except UnicodeDecodeError:
        pass

    # Genuinely binary — keep pysnmp's hex representation.
    try:
        return val.prettyPrint()
    except Exception:
        return str(val)


def _coerce_value(val: Any) -> Any:
    cls = type(val).__name__
    if cls in ("Integer", "Integer32", "Gauge32", "Counter32", "Counter64",
               "Unsigned32", "TimeTicks", "Integer64"):
        return int(val)
    if cls == "OctetString":
        return _clean_octet_string(val)
    if cls == "ObjectIdentifier":
        return str(val)
    if cls in ("Null", "NoSuchObject", "NoSuchInstance", "EndOfMibView"):
        return None
    try:
        return val.prettyPrint()
    except Exception:
        return str(val)
