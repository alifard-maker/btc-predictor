# Beta 1.0 — stable baseline (revert here)

**Tag:** `release/beta-1.0`  
**Commit:** `04a658e`  
**Live:** https://btc-predictor-production-f460.up.railway.app/dashboard

## Restore Beta 1.0

```bash
git fetch origin
git checkout release/beta-1.0
# redeploy on Railway from this ref
```

## What Beta 1.0 includes

- Trained LightGBM + daily 2am ET auto-retrain
- Kalshi `floor_strike` for t=0; BRTI live for P&amp;L
- Regime filter: **2+ flags** to veto open trades
- Late entry: **strict** whipsaw guard (`max_ref_crossings: 1`)
- Late-entry logging + separate calibration stats
- `min_remaining_minutes: 3` for late entry

## Does NOT include

- Dip-and-recover late entry (2× t=0 cross) — see **Beta 1.1**
