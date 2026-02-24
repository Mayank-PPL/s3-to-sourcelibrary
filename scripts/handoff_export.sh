#!/usr/bin/env bash
set -euo pipefail

# Creates a portable handoff bundle containing:
# - data/csv/* (inputs)
# - a consistent SQLite snapshot of data/index/migration_state.db (state)
#
# Usage:
#   scripts/handoff_export.sh [output_tgz]
#
# Example:
#   scripts/handoff_export.sh handoff/state_and_inputs.tgz

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

OUT_TGZ="${1:-handoff/state_and_inputs.tgz}"
OUT_TGZ_DIR="$(dirname "$OUT_TGZ")"

# Support both relative and absolute output paths
ARCHIVE_PATH="$OUT_TGZ"
if [[ "$OUT_TGZ" != /* ]]; then
  ARCHIVE_PATH="$ROOT_DIR/$OUT_TGZ"
fi

DB_PATH="data/index/migration_state.db"
CSV_DIR="data/csv"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "ERROR: sqlite3 is required but not found on PATH" >&2
  exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
  echo "ERROR: tar is required but not found on PATH" >&2
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "ERROR: state DB not found: $DB_PATH" >&2
  echo "Run indexing/migration first, or place the DB at that path." >&2
  exit 1
fi

if [[ ! -d "$CSV_DIR" ]]; then
  echo "ERROR: CSV directory not found: $CSV_DIR" >&2
  exit 1
fi

if ! find "$CSV_DIR" -maxdepth 1 -type f \( -name '*.csv' -o -name '*.csv.zip' -o -name '*.csv.gz' \) | grep -q .; then
  echo "ERROR: No CSV inputs found in $CSV_DIR" >&2
  exit 1
fi

mkdir -p "$OUT_TGZ_DIR"

STAGING_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$STAGING_DIR"
}
trap cleanup EXIT

mkdir -p "$STAGING_DIR/data/csv" "$STAGING_DIR/data/index"

# Copy inputs (preserve timestamps/permissions where possible)
# Using tar pipe avoids platform-specific cp flags.
( cd "$ROOT_DIR" && tar -cf - "data/csv" ) | ( cd "$STAGING_DIR" && tar -xf - )

# Create a consistent snapshot of the SQLite DB.
# This is safer than copying migration_state.db directly (WAL/journal considerations).
sqlite3 "$DB_PATH" ".backup '$STAGING_DIR/data/index/migration_state.db'"

# Create bundle
rm -f "$ARCHIVE_PATH" "${ARCHIVE_PATH}.sha256" 2>/dev/null || true
( cd "$STAGING_DIR" && tar -czf "$ARCHIVE_PATH" data/csv data/index/migration_state.db )

# Create a checksum file (works on macOS and Linux)
if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$ARCHIVE_PATH" > "${ARCHIVE_PATH}.sha256"
elif command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$ARCHIVE_PATH" > "${ARCHIVE_PATH}.sha256"
fi

echo "Created handoff bundle: $ARCHIVE_PATH"
if [[ -f "${ARCHIVE_PATH}.sha256" ]]; then
  echo "Wrote checksum: ${ARCHIVE_PATH}.sha256"
fi

echo "Next: send the .tgz (and .sha256) to the other person."
