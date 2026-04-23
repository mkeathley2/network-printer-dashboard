#!/usr/bin/env python3
"""
Network Printer Dashboard — Remote Agent
Version: 1.0.0

Standalone script deployed at remote sites. Scans local subnets via SNMP,
collects toner/status data, and reports to the central dashboard.

Dependencies: pysnmp>=6.2, requests
Install:  pip install pysnmp requests

Usage:
  python printer_agent.py                  # service mode (loop forever)
  python printer_agent.py --once           # single scan then exit
  python printer_agent.py --setup \
      --url https://printers.co.com \
      --key ABC123 \
      --subnet 192.168.10.0/24 \
      --location "Station 12"              # non-interactive setup
  python printer_agent.py --setup          # interactive setup wizard
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import pathlib
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
        walk_cmd,
    )
except ImportError:
    print("ERROR: 'pysnmp' is not installed. Run: pip install pysnmp", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("printer_agent")

_SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()
_CONFIG_PATH = _SCRIPT_DIR / "agent_config.json"
_LOG_PATH = _SCRIPT_DIR / "agent.log"

# File handler (appended once config dir is known)
_file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_file_handler)

AGENT_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# OIDs
# ---------------------------------------------------------------------------
OID_SYSDESCR             = "1.3.6.1.2.1.1.1.0"
OID_SYSOID               = "1.3.6.1.2.1.1.2.0"
OID_SYSNAME              = "1.3.6.1.2.1.1.5.0"
OID_SYSUPTIME            = "1.3.6.1.2.1.1.3.0"
OID_HR_DEVICE_DESCR      = "1.3.6.1.2.1.25.3.2.1.3.1"
OID_HR_DEVICE_STATUS     = "1.3.6.1.2.1.25.3.2.1.5.1"
OID_PRT_LIFE_COUNT       = "1.3.6.1.2.1.43.10.2.1.4.1.1"
OID_PRT_SERIAL           = "1.3.6.1.2.1.43.5.1.1.17.1"
OID_PRT_SUPPLIES_TABLE   = "1.3.6.1.2.1.43.11.1.1"

VENDOR_OID_PREFIXES = {
    "1.3.6.1.4.1.11.":   "hp",
    "1.3.6.1.4.1.2435.": "brother",
    "1.3.6.1.4.1.1602.": "canon",
    "1.3.6.1.4.1.1347.": "kyocera",
    "1.3.6.1.4.1.367.":  "ricoh",
}

SUPPLY_TYPE_MAP = {
    3: "tonerCartridge",
    4: "inkCartridge",
    7: "opc",
    10: "opc",
}

COLOR_MAP = {
    "black": "black", "k": "black", "bk": "black",
    "cyan": "cyan", "c": "cyan",
    "magenta": "magenta", "m": "magenta",
    "yellow": "yellow", "y": "yellow",
}


# ---------------------------------------------------------------------------
# Low-level SNMP helpers
# ---------------------------------------------------------------------------

def _coerce(val) -> object:
    cls = type(val).__name__
    if cls in ("Integer", "Integer32", "Gauge32", "Counter32", "Counter64",
               "Unsigned32", "TimeTicks"):
        return int(val)
    if cls == "OctetString":
        try:
            return val.prettyPrint()
        except Exception:
            return str(val)
    if cls in ("Null", "NoSuchObject", "NoSuchInstance", "EndOfMibView"):
        return None
    try:
        return val.prettyPrint()
    except Exception:
        return str(val)


async def _async_get(ip: str, oids: list[str], community: str, timeout: int, retries: int) -> dict:
    result: dict = {}
    engine = SnmpEngine()
    auth = CommunityData(community, mpModel=1)
    try:
        transport = await UdpTransportTarget.create((ip, 161), timeout=timeout, retries=retries)
    except Exception:
        return result
    for oid in oids:
        try:
            err_ind, err_status, _, var_binds = await get_cmd(
                engine, auth, transport, ContextData(),
                ObjectType(ObjectIdentity(oid)),
            )
            if err_ind:
                break
            if err_status:
                continue
            for vb in var_binds:
                result[str(vb[0])] = _coerce(vb[1])
        except Exception:
            break
    return result


async def _async_walk(ip: str, base_oid: str, community: str, timeout: int, retries: int) -> list:
    rows = []
    engine = SnmpEngine()
    auth = CommunityData(community, mpModel=1)
    try:
        transport = await UdpTransportTarget.create((ip, 161), timeout=timeout, retries=retries)
    except Exception:
        return rows
    try:
        async for err_ind, err_status, _, var_binds in walk_cmd(
            engine, auth, transport, ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if err_ind or err_status:
                break
            for vb in var_binds:
                rows.append((str(vb[0]), _coerce(vb[1])))
    except Exception:
        pass
    return rows


def snmp_get(ip: str, oids: list[str], community: str, timeout: int = 3, retries: int = 1) -> dict:
    try:
        return asyncio.run(_async_get(ip, oids, community, timeout, retries))
    except Exception:
        return {}


def snmp_walk(ip: str, base_oid: str, community: str, timeout: int = 3, retries: int = 1) -> list:
    try:
        return asyncio.run(_async_walk(ip, base_oid, community, timeout, retries))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Printer detection helpers
# ---------------------------------------------------------------------------

def _detect_vendor(sysoid: Optional[str], sysdescr: Optional[str]) -> str:
    if sysoid:
        for prefix, vendor in VENDOR_OID_PREFIXES.items():
            if sysoid.startswith(prefix):
                return vendor
    if sysdescr:
        low = sysdescr.lower()
        if "hp" in low or "hewlett" in low or "laserjet" in low:
            return "hp"
        if "brother" in low:
            return "brother"
        if "canon" in low:
            return "canon"
        if "kyocera" in low or "ecosys" in low:
            return "kyocera"
        if "ricoh" in low or "aficio" in low:
            return "ricoh"
    return "generic"


def _color_from_desc(desc: str) -> str:
    if not desc:
        return "unknown"
    low = desc.lower()
    for color in ("black", "cyan", "magenta", "yellow"):
        if color in low:
            return color
    if " k " in low or low.endswith(" k") or low.startswith("k "):
        return "black"
    import re
    m = re.search(r"[-\d]([cmyk])$", low)
    if m:
        return {"c": "cyan", "m": "magenta", "y": "yellow", "k": "black"}[m.group(1)]
    return "unknown"


def _parse_supplies(walk_rows: list) -> list[dict]:
    by_index: dict[int, dict] = {}
    for oid_str, value in walk_rows:
        parts = oid_str.rstrip(".").split(".")
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[-1])
            col = int(parts[-3])
        except (ValueError, IndexError):
            continue
        if idx not in by_index:
            by_index[idx] = {}
        if col == 4:
            by_index[idx]["type_int"] = int(value) if value is not None else None
        elif col == 6:
            by_index[idx]["description"] = str(value) if value else ""
        elif col == 8:
            by_index[idx]["max_cap"] = int(value) if value is not None else None
        elif col == 9:
            by_index[idx]["level"] = int(value) if value is not None else None

    result = []
    for idx, info in sorted(by_index.items()):
        level = info.get("level")
        max_cap = info.get("max_cap")
        desc = info.get("description", "")
        type_int = info.get("type_int")

        level_pct = None
        if level is not None and max_cap and max_cap > 0 and level >= 0:
            level_pct = max(0, min(100, round(level / max_cap * 100)))

        result.append({
            "supply_type": SUPPLY_TYPE_MAP.get(type_int, "tonerCartridge") if type_int else "tonerCartridge",
            "color": _color_from_desc(desc),
            "description": desc,
            "level_current": level,
            "level_max": max_cap,
            "level_pct": level_pct,
        })

    # Solo toner with unknown color → must be black
    toners = [s for s in result if s["supply_type"] == "tonerCartridge" and s["color"] == "unknown"]
    if len(toners) == 1:
        toners[0]["color"] = "black"

    return result


# ---------------------------------------------------------------------------
# Probe a single printer
# ---------------------------------------------------------------------------

def probe_printer(ip: str, community: str, timeout: int = 3, retries: int = 1) -> Optional[dict]:
    """
    Full SNMP probe of one IP.
    Returns a printer dict (matching checkin payload schema) or None if not a printer.
    """
    sys_result = snmp_get(
        ip,
        [OID_SYSDESCR, OID_SYSOID, OID_SYSNAME, OID_SYSUPTIME,
         OID_HR_DEVICE_DESCR, OID_HR_DEVICE_STATUS,
         OID_PRT_LIFE_COUNT, OID_PRT_SERIAL],
        community, timeout, retries,
    )

    if not sys_result:
        return None  # No SNMP response

    def _first(oid_prefix: str):
        prefix = oid_prefix.lstrip(".")
        for k, v in sys_result.items():
            if k.lstrip(".").startswith(prefix):
                return v
        return None

    sysdescr = _first(OID_SYSDESCR)
    sysoid = _first(OID_SYSOID)
    sysname = _first(OID_SYSNAME)
    uptime_raw = _first(OID_SYSUPTIME)
    hr_descr = _first(OID_HR_DEVICE_DESCR)
    page_raw = _first(OID_PRT_LIFE_COUNT)
    serial_raw = _first(OID_PRT_SERIAL)

    vendor = _detect_vendor(str(sysoid) if sysoid else None,
                             str(sysdescr) if sysdescr else None)

    model = (str(hr_descr).strip() if hr_descr else None) or (
        str(sysdescr).strip().split("\n")[0][:255] if sysdescr else None
    )

    uptime_seconds = None
    if uptime_raw is not None:
        try:
            uptime_seconds = int(uptime_raw) // 100
        except (ValueError, TypeError):
            pass

    page_count = None
    if page_raw is not None:
        try:
            page_count = int(page_raw)
        except (ValueError, TypeError):
            pass

    # Walk supplies table
    walk_rows = snmp_walk(ip, OID_PRT_SUPPLIES_TABLE, community, timeout, retries)
    supplies = _parse_supplies(walk_rows)

    # We consider it a printer if it has supply data OR a model containing printer keywords
    is_printer = bool(supplies)
    if not is_printer and model:
        kw = ("print", "laser", "inkjet", "mfp", "mfc", "scanner")
        is_printer = any(k in model.lower() for k in kw)
    if not is_printer:
        return None  # Device responded but looks like a switch/router/etc.

    try:
        hostname = socket.gethostbyaddr(ip)[0]
    except Exception:
        hostname = None

    return {
        "ip": ip,
        "vendor": vendor,
        "model": model,
        "serial": str(serial_raw).strip() if serial_raw else None,
        "hostname": hostname or (str(sysname).strip() if sysname else None),
        "is_online": True,
        "page_count": page_count,
        "uptime_seconds": uptime_seconds,
        "supplies": supplies,
    }


# ---------------------------------------------------------------------------
# Subnet discovery
# ---------------------------------------------------------------------------

async def _scan_one(ip: str, community: str, timeout: int) -> Optional[str]:
    """Quick check: returns ip if SNMP responds, None otherwise."""
    result = await _async_get(ip, [OID_SYSDESCR], community, timeout, 0)
    return ip if result else None


async def _discover_async(
    cidr: str,
    community: str,
    timeout: int = 1,
    max_concurrent: int = 50,
) -> list[str]:
    network = ipaddress.ip_network(cidr, strict=False)
    hosts = [str(h) for h in network.hosts()]

    sem = asyncio.Semaphore(max_concurrent)

    async def bounded(ip: str) -> Optional[str]:
        async with sem:
            return await _scan_one(ip, community, timeout)

    tasks = [bounded(ip) for ip in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, str)]


def discover_subnet(
    cidr: str,
    community: str = "public",
    timeout: int = 1,
    max_concurrent: int = 50,
) -> list[str]:
    """Return list of IPs that respond to SNMP in the given CIDR."""
    logger.info("Discovering %s …", cidr)
    try:
        return asyncio.run(_discover_async(cidr, community, timeout, max_concurrent))
    except Exception as exc:
        logger.error("Discovery error on %s: %s", cidr, exc)
        return []


# ---------------------------------------------------------------------------
# Checkin
# ---------------------------------------------------------------------------

def checkin(printers: list[dict], errors: list[dict], cfg: dict) -> Optional[str]:
    """
    POST printer data to the dashboard.
    Returns a command string (e.g. 'rescan', 'update', 'uninstall', 'config') or None.
    """
    url = cfg["dashboard_url"].rstrip("/") + "/api/agent/checkin"
    payload = {
        "agent_version": AGENT_VERSION,
        "scanned_at": datetime.now(tz=timezone.utc).isoformat(),
        "subnet": ", ".join(cfg.get("subnets", [])),
        "location_name": cfg.get("location", ""),
        "printers": printers,
        "errors": errors,
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"X-Agent-Key": cfg["api_key"]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        command = data.get("command")
        if command:
            logger.info("Received command from dashboard: %s", command)
        # Handle config command: update local config
        if command == "config" and "config" in data:
            for k, v in data["config"].items():
                cfg[k] = v
            save_config(cfg)
            logger.info("Config updated from dashboard: %s", data["config"])
        return command
    except requests.exceptions.ConnectionError:
        logger.warning("Could not connect to dashboard at %s — will retry next cycle", url)
    except requests.exceptions.Timeout:
        logger.warning("Dashboard checkin timed out — will retry next cycle")
    except Exception as exc:
        logger.error("Checkin error: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Self-update
# ---------------------------------------------------------------------------

def self_update(cfg: dict) -> None:
    """Download new agent.py from the dashboard and restart the service."""
    url = cfg["dashboard_url"].rstrip("/") + "/api/agent/download/agent.py"
    try:
        resp = requests.get(
            url,
            headers={"X-Agent-Key": cfg["api_key"]},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Self-update download failed: %s", exc)
        return

    new_path = pathlib.Path(__file__).with_suffix(".py.new")
    new_path.write_bytes(resp.content)
    # Atomic replace
    new_path.replace(pathlib.Path(__file__))
    logger.info("Agent script updated. Restarting service…")

    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["powershell", "-Command",
                              "Stop-ScheduledTask -TaskName PrinterAgent -ErrorAction SilentlyContinue; "
                              "Start-Sleep 2; Start-ScheduledTask -TaskName PrinterAgent"])
        else:
            subprocess.Popen(["sudo", "systemctl", "restart", "printer-agent"])
    except Exception as exc:
        logger.error("Service restart failed: %s — please restart manually", exc)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def uninstall() -> None:
    """Remove the agent service, config, and script, then exit."""
    logger.info("Uninstall command received — removing agent…")
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.run(["powershell", "-Command",
                            "Stop-ScheduledTask -TaskName PrinterAgent -ErrorAction SilentlyContinue; "
                            "Unregister-ScheduledTask -TaskName PrinterAgent -Confirm:$false -ErrorAction SilentlyContinue"],
                           check=False)
        else:
            subprocess.run(["sudo", "systemctl", "disable", "--now", "printer-agent"], check=False)
            subprocess.run(["sudo", "rm", "-f", "/etc/systemd/system/printer-agent.service"],
                           check=False)
            subprocess.run(["sudo", "systemctl", "daemon-reload"], check=False)
    except Exception as exc:
        logger.error("Service removal error: %s", exc)

    # Delete config and self
    try:
        _CONFIG_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    logger.info("Agent uninstalled. Exiting.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        logger.error("Config file not found: %s — run with --setup first", _CONFIG_PATH)
        sys.exit(1)
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def setup_config(args) -> dict:
    """Interactive or flag-driven first-time config wizard."""
    print("\n=== Network Printer Dashboard — Agent Setup ===\n")

    def _ask(prompt: str, default: str, arg_val: Optional[str] = None) -> str:
        if arg_val:
            print(f"{prompt}: {arg_val}")
            return arg_val
        val = input(f"{prompt} [{default}]: ").strip()
        return val or default

    url = _ask("Dashboard URL", "https://printers.yourcompany.com", getattr(args, "url", None))
    key = _ask("API Key", "", getattr(args, "key", None))
    subnet = _ask("Subnet to scan (CIDR)", "192.168.1.0/24", getattr(args, "subnet", None))
    location = _ask("Location name (optional)", "", getattr(args, "location", None))
    community = _ask("SNMP community string", "public", None)
    interval = _ask("Scan interval (minutes)", "60", None)

    cfg = {
        "dashboard_url": url.rstrip("/"),
        "api_key": key,
        "subnets": [subnet],
        "location": location,
        "snmp_community": community,
        "snmp_timeout": 3,
        "snmp_retries": 1,
        "scan_interval_minutes": int(interval),
        "agent_version": AGENT_VERSION,
    }
    save_config(cfg)
    print(f"\nConfig saved to {_CONFIG_PATH}")
    return cfg


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(cfg: dict) -> None:
    """Run one complete scan + checkin cycle."""
    community = cfg.get("snmp_community", "public")
    snmp_timeout = cfg.get("snmp_timeout", 3)
    snmp_retries = cfg.get("snmp_retries", 1)

    all_printers: list[dict] = []
    all_errors: list[dict] = []

    for subnet in cfg.get("subnets", []):
        live_ips = discover_subnet(subnet, community, timeout=1, max_concurrent=50)
        logger.info("Found %d SNMP-responsive hosts in %s", len(live_ips), subnet)

        for ip in live_ips:
            try:
                result = probe_printer(ip, community, snmp_timeout, snmp_retries)
                if result:
                    all_printers.append(result)
                    logger.info("  Printer: %s  %s  supplies=%d",
                                ip, result.get("model", "?"), len(result.get("supplies", [])))
            except Exception as exc:
                logger.error("  Error probing %s: %s", ip, exc)
                all_errors.append({"ip": ip, "error": str(exc)})

    logger.info("Checkin: %d printer(s), %d error(s)", len(all_printers), len(all_errors))
    command = checkin(all_printers, all_errors, cfg)

    if command == "update":
        self_update(cfg)
    elif command == "uninstall":
        uninstall()
    elif command == "rescan":
        logger.info("Rescan command received — running again immediately")
        run_once(cfg)


def main_loop(cfg: dict) -> None:
    logger.info("Agent starting. Version %s. Subnets: %s",
                AGENT_VERSION, cfg.get("subnets"))
    while True:
        try:
            run_once(cfg)
        except Exception:
            logger.exception("Unexpected error in scan cycle")
        interval = cfg.get("scan_interval_minutes", 60) * 60
        logger.info("Sleeping %d minutes until next scan…", cfg.get("scan_interval_minutes", 60))
        time.sleep(interval)
        # Reload config in case it was updated by a "config" command
        try:
            cfg = load_config()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Network Printer Dashboard Remote Agent")
    parser.add_argument("--once", action="store_true",
                        help="Run a single scan then exit (useful for testing)")
    parser.add_argument("--setup", action="store_true",
                        help="Run first-time configuration wizard")
    parser.add_argument("--url",      help="Dashboard URL (non-interactive setup)")
    parser.add_argument("--key",      help="API key (non-interactive setup)")
    parser.add_argument("--subnet",   help="Subnet to scan, e.g. 192.168.1.0/24")
    parser.add_argument("--location", help="Location name, e.g. 'Station 12'")
    args = parser.parse_args()

    if args.setup:
        cfg = setup_config(args)
        if not args.once:
            return  # Non-interactive setup from installer: just write config
    else:
        cfg = load_config()

    if args.once:
        run_once(cfg)
    else:
        main_loop(cfg)


if __name__ == "__main__":
    main()
