# BTC Predictor — Beta 1.0

**Tagged:** `beta-1.0`  
**Branch backup:** `backup/beta-1.0`

## Snapshot includes

- 15-minute ET slot predictions (:00, :15, :30, :45)
- Multi-scale features (1h / 4h / 12h context)
- Signal breakdown indicators on dashboard
- Rolling hit rate (deduplicated per slot): 1h, 2h, 4h, 12h
- Intra-slot exit guidance (HOLD / TAKE PROFIT / CUT LOSS)
- Coinbase BTC-USD live price feed
- Kalshi settlement note (CF Benchmarks BRTI)
- Reference price at t=0 from 1m slot open
- Embedded dashboard at `/dashboard`
- Railway + PostgreSQL deployment

## Live deployment

- https://btc-predictor-production-f460.up.railway.app/dashboard

## Restore from tag

```bash
git checkout beta-1.0
```

## Archive backup

A tarball `btc-predictor-beta-1.0-backup.tar.gz` is saved alongside this repo in `Documents/`.
