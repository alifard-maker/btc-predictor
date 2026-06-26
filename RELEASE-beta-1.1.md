# BTC Predictor — Beta 1.1

**Tagged:** `beta-1.1`  
**Branch backup:** `backup/beta-1.1`

## Snapshot includes

Everything in Beta 1.0, plus:

- Dashboard login (`APP_PASSWORD`)
- t=0 price override (manual entry when API is off)
- Intra-slot reassessment on CUT LOSS / TAKE PROFIT
- Coinbase live tick at slot open + prior-minute close for t=0
- Live “Now” price with trade timestamp
- **P&L shows % and $ per 1 BTC**
- **Version label on dashboard**

## Live deployment

- https://btc-predictor-production-f460.up.railway.app/dashboard

## Restore from tag

```bash
git checkout beta-1.1
```

## Archive backup

Tarball: `btc-predictor-beta-1.1-backup.tar.gz` in `Documents/`.
