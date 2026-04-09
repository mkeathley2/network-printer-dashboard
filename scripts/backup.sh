#!/usr/bin/env bash
# backup.sh — Dump the printerdb MariaDB database to a timestamped gzipped SQL file.
# Usage: ./scripts/backup.sh
# Requires: DB_PASSWORD env var (or source .env first)
# Example:  source .env && ./scripts/backup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="${PROJECT_DIR}/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_FILE="${BACKUP_DIR}/printerdb_${TIMESTAMP}.sql.gz"

mkdir -p "${BACKUP_DIR}"

echo "==> Starting backup of printerdb..."

docker compose -f "${PROJECT_DIR}/docker-compose.yml" exec -T db \
    mysqldump \
    -u printer_app \
    -p"${DB_PASSWORD}" \
    --single-transaction \
    --routines \
    --triggers \
    --add-drop-table \
    printerdb \
    | gzip > "${OUTPUT_FILE}"

SIZE=$(du -sh "${OUTPUT_FILE}" | cut -f1)
echo "==> Backup complete: ${OUTPUT_FILE} (${SIZE})"
