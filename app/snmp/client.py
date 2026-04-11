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


def _coerce_value(val: Any) -> Any:
    cls = type(val).__name__
    if cls in ("Integer", "Integer32", "Gauge32", "Counter32", "Counter64",
               "Unsigned32", "TimeTicks", "Integer64"):
        return int(val)
    if cls == "OctetString":
        try:
            return val.prettyPrint()
        except Exception:
            return str(val)
    if cls == "ObjectIdentifier":
        return str(val)
    if cls in ("Null", "NoSuchObject", "NoSuchInstance", "EndOfMibView"):
        return None
    try:
        return val.prettyPrint()
    except Exception:
        return str(val)
