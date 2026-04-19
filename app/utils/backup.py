"""
Backup and restore utilities for the Printer Dashboard.

Backup: exports all (or a subset of) DB tables to JSON files inside a zip.
Restore: reads that zip and bulk-inserts the data back, replacing existing rows.

Backup scopes
  "config"  — everything except telemetry_snapshots, supply_snapshots, alert_events
  "full"    — all tables including historical snapshots
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, date
from typing import Any

from sqlalchemy import inspect, text

from app.core.database import db
from app.utils.version import get_current_version

logger = logging.getLogger(__name__)

# Restore order respects FK constraints (parents before children)
_ALL_TABLES = [
    "site_settings",
    "users",
    "locations",
    "printer_groups",
    "printers",
    "telemetry_snapshots",   # full only
    "supply_snapshots",      # full only
    "alert_events",          # full only
    "alert_state",
    "discovery_scans",
    "discovery_results",
    "printer_import_data",
    "audit_log",
]

_HISTORY_TABLES = {"telemetry_snapshots", "supply_snapshots", "alert_events"}

_CONFIG_TABLES = [t for t in _ALL_TABLES if t not in _HISTORY_TABLES]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _json_default(obj: Any) -> Any:
    """JSON serialiser that handles datetime/date objects."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


def _row_count(table: str) -> int:
    try:
        result = db.session.execute(text(f"SELECT COUNT(*) FROM `{table}`"))
        return result.scalar() or 0
    except Exception:
        return 0


def _estimate_mb(scope: str) -> float:
    """
    Rough compressed-size estimate in MB.
    Bytes per row: telemetry ~250, supply ~180, others ~200.
    Compression ratio: ~0.25 (JSON compresses well).
    """
    sizes = {
        "telemetry_snapshots": 250,
        "supply_snapshots": 180,
    }
    tables = _ALL_TABLES if scope == "full" else _CONFIG_TABLES
    total_bytes = sum(
        _row_count(t) * sizes.get(t, 200) for t in tables
    )
    compressed = total_bytes * 0.25
    return round(compressed / (1024 * 1024), 1)


def get_backup_stats() -> dict:
    """Return row counts and size estimates for the UI."""
    counts = {t: _row_count(t) for t in _ALL_TABLES}
    return {
        "counts": counts,
        "config_size_mb": _estimate_mb("config"),
        "full_size_mb": _estimate_mb("full"),
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_zip(scope: str = "config") -> bytes:
    """
    Export the database to a zip of JSON files.
    scope: "config" or "full"
    Returns raw zip bytes ready to send as a download.
    """
    tables = _ALL_TABLES if scope == "full" else _CONFIG_TABLES
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    folder = f"printer-dashboard-backup-{timestamp}"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Build manifest
        manifest = {
            "app_version": get_current_version(),
            "scope": scope,
            "created_at": datetime.utcnow().isoformat(),
            "tables": {},
        }

        insp = inspect(db.engine)
        for table in tables:
            if table not in insp.get_table_names():
                logger.debug("Skipping table %s (not found)", table)
                continue
            try:
                rows = db.session.execute(text(f"SELECT * FROM `{table}`")).mappings().all()
                row_list = [dict(r) for r in rows]
                manifest["tables"][table] = len(row_list)
                json_bytes = json.dumps(row_list, default=_json_default, ensure_ascii=False).encode()
                zf.writestr(f"{folder}/{table}.json", json_bytes)
            except Exception as exc:
                logger.error("Failed to export table %s: %s", table, exc)
                manifest["tables"][table] = f"ERROR: {exc}"

        zf.writestr(
            f"{folder}/manifest.json",
            json.dumps(manifest, indent=2).encode(),
        )

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Import / Restore
# ---------------------------------------------------------------------------
def import_zip(file_bytes: bytes) -> dict:
    """
    Restore the database from a backup zip.
    Returns {"tables_restored": [...], "rows_restored": N} on success.
    Raises ValueError on invalid zip / missing manifest.
    Raises RuntimeError on DB error.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Not a valid zip file: {exc}") from exc

    # Find manifest
    manifest_name = next((n for n in zf.namelist() if n.endswith("manifest.json")), None)
    if not manifest_name:
        raise ValueError("No manifest.json found in zip — this doesn't look like a valid backup.")

    manifest = json.loads(zf.read(manifest_name))
    if "tables" not in manifest or "scope" not in manifest:
        raise ValueError("manifest.json is missing required keys.")

    folder = manifest_name.replace("manifest.json", "")
    tables_in_backup = list(manifest["tables"].keys())
    # Only restore tables that actually exist as JSON files and in DB
    insp = inspect(db.engine)
    db_tables = set(insp.get_table_names())

    tables_restored = []
    rows_restored = 0

    try:
        with db.engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))

            for table in _ALL_TABLES:
                if table not in tables_in_backup:
                    continue
                if table not in db_tables:
                    continue
                json_name = f"{folder}{table}.json"
                if json_name not in zf.namelist():
                    continue

                rows = json.loads(zf.read(json_name))
                conn.execute(text(f"TRUNCATE TABLE `{table}`"))

                if rows:
                    # SQLAlchemy core insert via raw dicts
                    conn.execute(
                        text(_build_insert(table, rows[0].keys())),
                        rows,
                    )
                    rows_restored += len(rows)

                tables_restored.append(table)

            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    except Exception as exc:
        logger.exception("Restore failed")
        raise RuntimeError(str(exc)) from exc

    return {
        "tables_restored": tables_restored,
        "rows_restored": rows_restored,
        "scope": manifest.get("scope", "unknown"),
        "backup_version": manifest.get("app_version", "unknown"),
        "backup_date": manifest.get("created_at", "unknown"),
    }


def _build_insert(table: str, columns) -> str:
    cols = ", ".join(f"`{c}`" for c in columns)
    vals = ", ".join(f":{c}" for c in columns)
    return f"INSERT INTO `{table}` ({cols}) VALUES ({vals})"


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
def execute_reset(categories: list[str]) -> list[str]:
    """
    Wipe the selected categories. Returns list of human-readable descriptions of what was cleared.

    categories may include:
      "printers"   — printers + telemetry + supply + alert events + alert states
      "locations"  — locations table
      "discovery"  — discovery_scans + discovery_results
      "imports"    — printer_import_data
      "audit"      — audit_log
      "users"      — all users; re-seed admin/admin
      "settings"   — site_settings
    """
    from werkzeug.security import generate_password_hash

    cleared = []

    with db.engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))

        if "printers" in categories:
            for t in ("supply_snapshots", "telemetry_snapshots",
                      "alert_events", "alert_state", "printers"):
                conn.execute(text(f"TRUNCATE TABLE `{t}`"))
            cleared.append("Printers & monitoring history")

        if "locations" in categories:
            conn.execute(text("UPDATE printers SET location_id = NULL"))
            conn.execute(text("TRUNCATE TABLE locations"))
            cleared.append("Locations")

        if "discovery" in categories:
            conn.execute(text("TRUNCATE TABLE discovery_results"))
            conn.execute(text("TRUNCATE TABLE discovery_scans"))
            cleared.append("Discovery history")

        if "imports" in categories:
            conn.execute(text("TRUNCATE TABLE printer_import_data"))
            cleared.append("Staged import data")

        if "audit" in categories:
            conn.execute(text("TRUNCATE TABLE audit_log"))
            cleared.append("Audit log")

        if "users" in categories:
            conn.execute(text("TRUNCATE TABLE users"))
            # Re-seed admin
            conn.execute(text(
                "INSERT INTO users (username, password_hash, role) VALUES "
                "(:u, :p, 'admin')"
            ), {"u": "admin", "p": generate_password_hash("admin")})
            cleared.append("Users (admin/admin restored)")

        if "settings" in categories:
            conn.execute(text("TRUNCATE TABLE site_settings"))
            cleared.append("Settings (all defaults restored)")

        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    return cleared
