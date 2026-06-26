#!/usr/bin/env bash
# Set KALSHI_PRIVATE_KEY on Railway from your Kalshi API .key file.
# Usage: ./scripts/set-kalshi-private-key-railway.sh /path/to/your-kalshi.key
set -euo pipefail
KEY_FILE="${1:?Usage: $0 /path/to/kalshi-private.key}"
if [[ ! -f "$KEY_FILE" ]]; then
  echo "File not found: $KEY_FILE" >&2
  exit 1
fi
cd "$(dirname "$0")/.."
railway variables set "KALSHI_PRIVATE_KEY=$(cat "$KEY_FILE")"
echo "KALSHI_PRIVATE_KEY set. Redeploying..."
railway up --detach 2>/dev/null || railway redeploy 2>/dev/null || echo "Trigger redeploy from Railway dashboard if needed."
echo "After deploy, check: curl -s https://btc-predictor-production-f460.up.railway.app/health | jq .kalshi.authenticated"
