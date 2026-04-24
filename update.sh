#!/usr/bin/env bash
#
# Update the Network Printer Dashboard on this host.
# Run from the repo root:  ./update.sh
#
# What it does:
#   1. git pull --ff-only  (bails if you have local changes that would conflict)
#   2. docker compose up --build -d  (rebuild + recreate containers)
#   3. Print the new version.
#
set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

echo "==> Current version: $(cat VERSION 2>/dev/null || echo unknown)"
echo "==> Pulling latest from GitHub..."
git pull --ff-only

echo "==> Rebuilding and restarting containers..."
docker compose up --build -d

echo "==> Done.  New version: $(cat VERSION 2>/dev/null || echo unknown)"
echo "    Check status with:  docker compose ps"
echo "    View logs with:     docker compose logs -f app"
