#!/usr/bin/env bash
# Run on the LXC to pull the latest fixtures_import.csv and trigger the upload.
# Schedule this with cron, or call it from your PNFC upload workflow.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
IMPORT_CSV="$REPO_DIR/fixtures_import.csv"
PNFC_DIR="${PNFC_DIR:-/opt/pnfc}"   # override with env var if needed

echo "[$(date -Is)] Pulling latest from origin..."
git -C "$REPO_DIR" pull --ff-only origin main

echo "[$(date -Is)] fixtures_import.csv updated. Launching PNFC upload..."
# Adjust the command below to match how your PNFC upload workflow is invoked:
# e.g.  python "$PNFC_DIR/upload.py" --input "$IMPORT_CSV"
#        or: node "$PNFC_DIR/import.js" "$IMPORT_CSV"
echo "TODO: replace this line with your PNFC upload command"
echo "  Import CSV: $IMPORT_CSV"
