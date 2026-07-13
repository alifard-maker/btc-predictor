#!/usr/bin/env bash
# Download /data/backups/live/ from Railway into local data/backups/live/
set -euo pipefail
cd "$(dirname "$0")/.."

API_BASE="${RAILWAY_API_URL:-https://btc-predictor-production-f460.up.railway.app}"
DEST_ROOT="${1:-data/backups}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

mkdir -p "$DEST_ROOT"

_sync_via_api() {
  if [ -z "${ADMIN_API_KEY:-}" ]; then
    return 1
  fi
  local tmp
  tmp="$(mktemp /tmp/btc-predictor-live-backup-XXXX.zip)"
  if curl -fsS \
    -H "X-Api-Key: $ADMIN_API_KEY" \
    "$API_BASE/api/admin/backup-archive?mode=live" \
    -o "$tmp"; then
    unzip -o -q "$tmp" -d "$DEST_ROOT"
    rm -f "$tmp"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

_sync_via_railway_cli() {
  if ! command -v railway >/dev/null 2>&1; then
    return 1
  fi
  if ! railway whoami >/dev/null 2>&1; then
    return 1
  fi
  echo "Using railway run (tar from /data/backups/live) ..."
  railway run -- bash -lc 'cd /data/backups && tar czf - live' | tar xzf - -C "$DEST_ROOT"
}

echo "Downloading live backup from Railway ..."
if _sync_via_api; then
  :
elif _sync_via_railway_cli; then
  :
else
  echo "Could not sync. Either:" >&2
  echo "  1) Set ADMIN_API_KEY in .env and deploy >= 5.0.98 (backup-archive API), then re-run" >&2
  echo "  2) railway login && railway link && re-run (uses railway run + tar)" >&2
  exit 1
fi

echo ""
echo "Synced:"
find "$DEST_ROOT/live" -name 'trades.csv' -o -name 'TAX_README.txt' -o -name 'tax_manifest.json' 2>/dev/null | sort
echo ""
echo "Local tax folder: $(cd "$DEST_ROOT/live" && pwd)"
