#!/usr/bin/env bash
# Quick check: Railway volume at /data, Kalshi creds, tax CSV export counts.
set -euo pipefail
cd "$(dirname "$0")/.."

HEALTH_URL="${RAILWAY_HEALTH_URL:-https://btc-predictor-production-f460.up.railway.app/health}"
API_BASE="${RAILWAY_API_URL:-https://btc-predictor-production-f460.up.railway.app}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "=== Railway production status ==="
echo "Health: $HEALTH_URL"
echo ""

python3 - "$HEALTH_URL" "$API_BASE" "${ADMIN_API_KEY:-}" <<'PY'
import json
import sys
import urllib.request

health_url, api_base, admin_key = sys.argv[1:4]

with urllib.request.urlopen(health_url, timeout=30) as resp:
    h = json.load(resp)

print(f"Version:           {h.get('version')}")
print(f"DATA_DIR:          {h.get('data_dir')}")
print(f"Volume at /data:   {h.get('volume_mounted_at_data')}")
print(f"Railway mount:     {h.get('railway_volume_mount_path')}")
if h.get("data_persistence_warning"):
    print(f"WARNING:           {h['data_persistence_warning']}")

kalshi = h.get("kalshi") or {}
print(f"Kalshi auth:       {kalshi.get('authenticated')}")
if kalshi.get("authenticated"):
    print(f"Kalshi balance:    ${kalshi.get('balance_usd', '?')}")

lb = h.get("log_backup") or {}
print(f"Backup root:       {lb.get('backup_root')}")
print(f"Volume persistent: {lb.get('volume_persistent')}")

live = ((lb.get("last_run") or {}).get("live") or {})
print(f"Last tax export:   ok={live.get('ok')}  kalshi_rows={live.get('total_trades')}")
per_bot = live.get("per_bot") or {}
if per_bot:
    print("Per-bot Kalshi rows:")
    for bot, n in sorted(per_bot.items()):
        print(f"  {bot}: {n}")

tax = lb.get("tax_export") or {}
if tax.get("per_bot"):
    print("\nTax CSVs on disk:")
    for bot, info in sorted(tax["per_bot"].items()):
        if isinstance(info, dict):
            rows = info.get("kalshi_wallet_rows", 0)
            ok = "yes" if info.get("trades_csv") else "no"
            print(f"  {bot}: trades.csv={ok}  rows={rows}")

if not admin_key:
    print("\nTip: set ADMIN_API_KEY in .env for detailed /api/admin/backup-status")
    sys.exit(0)

req = urllib.request.Request(
    f"{api_base.rstrip('/')}/api/admin/backup-status",
    headers={"X-Api-Key": admin_key},
)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        admin = json.load(resp)
    print("\n=== Admin backup-status ===")
    print(json.dumps(admin.get("tax_export", {}), indent=2))
except urllib.error.HTTPError as e:
    print(f"\nAdmin backup-status failed: HTTP {e.code}")
PY

echo ""
echo "Sync tax CSVs to this Mac:"
echo "  ./scripts/sync-railway-tax-docs.sh"
