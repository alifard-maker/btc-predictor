#!/usr/bin/env bash
# Attach a persistent Railway volume at /data for btc-predictor (paper bankroll + bot logs).
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE_ID="${RAILWAY_SERVICE_ID:-7223bd13-6ce8-4a54-b814-b835fa26fb1b}"
MOUNT_PATH="/data"

if ! railway whoami >/dev/null 2>&1; then
  echo "Not logged in. Run: railway login" >&2
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

echo "Done. Verify: curl -s https://btc-predictor-production-f460.up.railway.app/health | jq '{scheduler_running, data_dir}'"
