#!/usr/bin/env bash
set -euo pipefail

# Imports a handoff bundle created by scripts/handoff_export.sh.
#
# Usage:
#   scripts/handoff_import.sh <path_to_tgz>
#
# Example:
#   scripts/handoff_import.sh /path/to/state_and_inputs.tgz

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BUNDLE_PATH="${1:-}"
if [[ -z "$BUNDLE_PATH" ]]; then
  echo "Usage: scripts/handoff_import.sh <path_to_tgz>" >&2
  exit 2
fi

if [[ ! -f "$BUNDLE_PATH" ]]; then
  echo "ERROR: bundle not found: $BUNDLE_PATH" >&2
  exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
  echo "ERROR: tar is required but not found on PATH" >&2
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "ERROR: sqlite3 is required but not found on PATH" >&2
  exit 1
fi

# Optional checksum verification if a sibling .sha256 exists
SHA_FILE="${BUNDLE_PATH}.sha256"
if [[ -f "$SHA_FILE" ]]; then
  echo "Found checksum file: $SHA_FILE"
  if command -v shasum >/dev/null 2>&1; then
    (cd "$(dirname "$BUNDLE_PATH")" && shasum -a 256 -c "$(basename "$SHA_FILE")")
  elif command -v sha256sum >/dev/null 2>&1; then
    (cd "$(dirname "$BUNDLE_PATH")" && sha256sum -c "$(basename "$SHA_FILE")")
  else
    echo "WARNING: no shasum/sha256sum available; skipping checksum verification" >&2
  fi
fi

mkdir -p data/csv data/index

echo "Extracting bundle into: $ROOT_DIR"
tar -xzf "$BUNDLE_PATH" -C "$ROOT_DIR"

DB_PATH="data/index/migration_state.db"
if [[ ! -f "$DB_PATH" ]]; then
  echo "ERROR: expected DB not found after extraction: $DB_PATH" >&2
  echo "Bundle may be malformed." >&2
  exit 1
fi

# Basic DB sanity check
INTEGRITY_RESULT="$(sqlite3 "$DB_PATH" "PRAGMA integrity_check;" | head -n 1)"
if [[ "$INTEGRITY_RESULT" != "ok" ]]; then
  echo "ERROR: SQLite integrity_check failed: $INTEGRITY_RESULT" >&2
  exit 1
fi

echo "Import complete. SQLite integrity_check: ok"

echo "Next: configure .env, then run:"
echo "  python -m src.main status"
echo "  python -m src.main migrate"
