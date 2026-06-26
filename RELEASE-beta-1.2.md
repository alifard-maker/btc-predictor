# Release beta 1.2 — daily retrain + late entry

**Tag:** `backup/release-daily-retrain-2026-06-28`  
**Live:** https://btc-predictor-production-f460.up.railway.app/dashboard

## Restore this version

```bash
git fetch origin
git checkout backup/release-daily-retrain-2026-06-28
```

## What's in this release

- **Trained LightGBM** on ~2y exchange OHLC; Kalshi BRTI for t=0, outcomes, calibration
- **Regime filter** — blocks only when 2+ flags align; single flags are advisory
- **Late entry** — WATCH → LATE LONG/SHORT on NO-TRADE slots after conservative gates
- **Kalshi floor_strike** for t=0 with retry at slot open
- **Daily auto-retrain** — 2:00 AM ET (first run: next calendar day after deploy)
- **Calibrator** — refits every 6h from resolved slots
- **Stats epoch** — reset via `POST /api/admin/reset-stats` for clean release tracking

## Ops

```bash
# Reset calibration baseline
curl -X POST -H "X-API-Key: $ADMIN_API_KEY" \
  "https://btc-predictor-production-f460.up.railway.app/api/admin/reset-stats?note=beta-1.2"

# Manual train (also runs daily at 2am ET)
curl -X POST -H "X-API-Key: $ADMIN_API_KEY" \
  "https://btc-predictor-production-f460.up.railway.app/api/admin/train"
```
