#!/usr/bin/env bash
# Attach a persistent Railway volume at /data for btc-predictor (paper bankroll + bot logs + backups).
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE_ID="${RAILWAY_SERVICE_ID:-7223bd13-6ce8-4a54-b814-b835fa26fb1b}"
MOUNT_PATH="/data"
HEALTH_URL="${RAILWAY_HEALTH_URL:-https://btc-predictor-production-f460.up.railway.app/health}"

if ! railway whoami >/dev/null 2>&1; then
  echo "=== Railway login required ===" >&2
  echo "Run:  railway login" >&2
  echo "Then: railway link   # select btc-predictor project" >&2
  echo "Then re-run this script." >&2
  exit 1
fi

if ! railway status >/dev/null 2>&1; then
  echo "=== Link this repo to your Railway project ===" >&2
  echo "Run:  railway link" >&2
  exit 1
fi

echo "Checking existing volumes for service ${SERVICE_ID}..."
EXISTING="$(railway volume list --json 2>/dev/null || echo '[]')"
if echo "$EXISTING" | grep -q "\"mountPath\":\"${MOUNT_PATH}\""; then
  echo "Volume already mounted at ${MOUNT_PATH}."
else
  echo "Adding volume at ${MOUNT_PATH}..."
  railway volume add --service "$SERVICE_ID" --mount-path "$MOUNT_PATH" --json
fi

echo "Redeploying so the volume is attached..."
railway redeploy --yes 2>/dev/null || railway up --detach

echo ""
echo "Waiting 45s for deploy..."
sleep 45

echo "Health check:"
if command -v jq >/dev/null 2>&1; then
  curl -fsS "$HEALTH_URL" | jq '{volume_mounted_at_data, data_dir, data_persistence_warning, log_backup}'
else
  curl -fsS "$HEALTH_URL"
fi

echo ""
echo "Done. volume_mounted_at_data must be true or redeploys will keep wiping bot data."
