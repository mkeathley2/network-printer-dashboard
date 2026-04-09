#!/usr/bin/env bash
# restore.sh — Restore printerdb from a gzipped SQL backup file.
# Usage: ./scripts/restore.sh <path/to/backup.sql.gz>
# Requires: DB_PASSWORD env var (or source .env first)
# Example:  source .env && ./scripts/restore.sh backups/printerdb_20260101_120000.sql.gz
#
# WARNING: This will OVERWRITE the current database contents.

set -euo pipefail

BACKUP_FILE="${1:?Usage: restore.sh <path/to/backup.sql.gz>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "ERROR: File not found: ${BACKUP_FILE}"
    exit 1
fi

echo "==> Restoring printerdb from: ${BACKUP_FILE}"
echo "    WARNING: This will overwrite all current data."
read -r -p "    Continue? [y/N] " confirm
if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
    echo "Aborted."
    exit 0
fi

gunzip -c "${BACKUP_FILE}" \
    | docker compose -f "${PROJECT_DIR}/docker-compose.yml" exec -T db \
        mysql \
        -u printer_app \
        -p"${DB_PASSWORD}" \
        printerdb

echo "==> Restore complete from ${BACKUP_FILE}"
