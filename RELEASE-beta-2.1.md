# Beta 2.1 — Daily / threshold tab

**Tag:** `release/beta-2.1`  
**Revert to:** `release/beta-2.0` (15m + flip only, no daily tab)

## What's new

- Dashboard **Daily / threshold** tab (separate from 15m slot)
- **Strategy 1:** Kalshi threshold (above/below) — model vs Kalshi YES mid using S/R, wicks, volume, terminal distribution
- **Strategy 2:** Range bands — consolidation box + band probabilities
- Uses `BTCD`/`BTC` daily series when open; falls back to `KXBTCD`/`KXBTC` hourly

## Config

`daily:` block in `config.yaml`
