#!/usr/bin/env bash
# Download /data/backups/live/ from Railway into local data/backups/live/
set -euo pipefail
cd "$(dirname "$0")/.."

API_BASE="${RAILWAY_API_URL:-https://btc-predictor-production-f460.up.railway.app}"
RAILWAY_SERVICE="${RAILWAY_SERVICE:-btc-predictor}"
DEST_ROOT="${1:-data/backups}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

_load_admin_key_from_railway() {
  if [ -n "${ADMIN_API_KEY:-}" ]; then
    return 0
  fi
  if ! command -v railway >/dev/null 2>&1 || ! railway whoami >/dev/null 2>&1; then
    return 1
  fi
  local key
  key="$(railway variable list --kv 2>/dev/null | sed -n 's/^ADMIN_API_KEY=//p' | head -1 || true)"
  if [ -n "$key" ]; then
    ADMIN_API_KEY="$key"
    export ADMIN_API_KEY
    echo "Using ADMIN_API_KEY from Railway variables (railway variable list)."
    return 0
  fi
  return 1
}

_sync_via_api() {
  _load_admin_key_from_railway || true
  if [ -z "${ADMIN_API_KEY:-}" ]; then
    echo "No ADMIN_API_KEY in .env and could not read it from Railway." >&2
    return 1
  fi
  local tmp http_code
  tmp="$(mktemp /tmp/btc-predictor-live-backup-XXXX.zip)"
  http_code="$(curl -sS -w "%{http_code}" -o "$tmp" \
    -H "X-Api-Key: $ADMIN_API_KEY" \
    "$API_BASE/api/admin/backup-archive?mode=live")" || {
    rm -f "$tmp"
    return 1
  }
  if [ "$http_code" != "200" ]; then
    echo "backup-archive API returned HTTP $http_code" >&2
    rm -f "$tmp"
    return 1
  fi
  unzip -o -q "$tmp" -d "$DEST_ROOT"
  rm -f "$tmp"
  echo "Synced via API ($API_BASE)."
  return 0
}

_sync_via_railway_ssh() {
  if ! command -v railway >/dev/null 2>&1; then
    return 1
  fi
  if ! railway whoami >/dev/null 2>&1; then
    echo "railway login required for SSH sync." >&2
    return 1
  fi
  echo "Using railway ssh on service ${RAILWAY_SERVICE} (tar from /data/backups/live) ..."
  # railway run is LOCAL only (no /data volume). ssh runs on the deployed container.
  railway ssh -s "$RAILWAY_SERVICE" -- tar czf - -C /data/backups live | tar xzf - -C "$DEST_ROOT"
  echo "Synced via railway ssh."
  return 0
}

mkdir -p "$DEST_ROOT"

echo "Downloading live backup from Railway ..."
if _sync_via_api; then
  :
elif _sync_via_railway_ssh; then
  :
else
  echo "Could not sync. Fix one of:" >&2
  echo "  1) Add ADMIN_API_KEY to .env (same as Railway), or railway login so the script can read it" >&2
  echo "  2) railway login && railway link  # then re-run (uses railway ssh + tar on the volume)" >&2
  exit 1
fi

echo ""
echo "Synced:"
find "$DEST_ROOT/live" -name 'trades.csv' -o -name 'TAX_README.txt' -o -name 'tax_manifest.json' 2>/dev/null | sort
echo ""
echo "Local tax folder: $(cd "$DEST_ROOT/live" && pwd)"
