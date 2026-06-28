# BTC Predictor — Beta 1.0

Probabilistic BTC direction assistant with calibration tracking, backtesting, and paper trading. **No real trading in Stage 1.**

Runs as a **Railway backend** — predictions, data collection, and scheduling happen in the cloud 24/7.

## Architecture

```
btc-predictor/
├── Dockerfile / railway.toml   # Railway deployment
├── src/api/main.py             # FastAPI + background scheduler
├── src/db/store.py             # PostgreSQL (Railway) or SQLite (local)
├── src/data/                   # ccxt fetcher + parquet on /data volume
├── src/features/               # Stage 1 + Phase 2 features
├── src/models/                 # Train + predict
├── src/trading/                # Edge, backtest, paper trader
└── scripts/                    # Local CLI tools
```

## Deploy to Railway

### 1. Push to GitHub

```bash
cd btc-predictor
git init && git add . && git commit -m "BTC predictor backend"
# push to your GitHub repo
```

### 2. Create Railway project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
2. Select your `btc-predictor` repo
3. Railway detects the `Dockerfile` automatically

### 3. Add PostgreSQL

1. In your Railway project → **+ New** → **Database** → **PostgreSQL**
2. Railway injects `DATABASE_URL` into your service automatically

### 4. Add a Volume (for candle history)

1. Click your service → **Settings** → **Volumes** → **Add Volume**
2. Mount path: `/data`
3. This persists parquet candle files across deploys

### 5. Set environment variables

| Variable | Value |
|----------|-------|
| `ADMIN_API_KEY` | A long random secret (for admin endpoints) |
| `DATA_DIR` | `/data` |
| `EXCHANGE` | `binance` (works on Railway servers) |
| `SYMBOL` | `BTC/USDT` |
| `ENABLE_SCHEDULER` | `true` |

`DATABASE_URL` is set automatically when you add Postgres.

### 6. Attach persistent volume (required for paper bots)

Bot bankroll, trade logs, and Auto-bet settings live in SQLite under `DATA_DIR` (default `/data`). **Attach a Railway volume at `/data`** or redeploys wipe paper state. See **[RAILWAY.md](RAILWAY.md)** for 24/7 paper bot setup.

### 7. Deploy

Railway builds the Docker image and starts the API. Health check: `GET /health`

Your live API will be at `https://your-app.up.railway.app`

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (Railway uses this) |
| GET | `/api/prediction/latest` | Latest UP/DOWN %, signal, expected move |
| GET | `/api/predictions?limit=50` | Recent prediction history |
| GET | `/api/calibration` | Calibration summary + bins |
| GET | `/api/status` | Exchange, candle count, model status |
| POST | `/api/predict/now` | Force a prediction (requires `X-Api-Key` header) |
| POST | `/api/admin/collect?years=3` | Start historical collection (requires API key) |

Interactive docs: `https://your-app.up.railway.app/docs`

### Example

```bash
curl https://your-app.up.railway.app/api/prediction/latest
```

```json
{
  "timestamp": "2026-06-25T23:06:00+00:00",
  "price": 59776.40,
  "prob_up": 0.673,
  "prob_down": 0.327,
  "confidence": 0.81,
  "expected_move": 145.0,
  "signal": "LONG"
}
```

## Local development

```bash
cd btc-predictor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run API locally (same as Railway)
uvicorn src.api.main:app --reload --port 8000

# Or CLI-only mode (no API)
python scripts/run_predictor.py --once
```

## Offline scripts (run locally or via Railway shell)

```bash
python scripts/collect_historical.py --years 3
python scripts/train.py --model-type lightgbm
python scripts/backtest.py
python scripts/calibration_report.py
```

Upload trained `model.joblib` to the `/data/models/` volume (or bake into a future deploy).

## Stages

| Stage | Status | Description |
|-------|--------|-------------|
| **1 — Prediction** | ✅ | 1m/15m features, 5m UP/DOWN %, LONG/SHORT/NO TRADE, full logging |
| **2 — Backtest** | ✅ | Walk-forward train/test, fee-adjusted edge |
| **3 — Paper trade** | ✅ | $100 bankroll, 1% risk, stop rules |
| **Railway backend** | ✅ | FastAPI + Postgres + volume + 24/7 scheduler |

## Configuration

Edit `config.yaml` or override via Railway env vars:

- `min_edge_confidence: 0.57` — only trade above this after fees
- `EXCHANGE` / `SYMBOL` — data source
- `ADMIN_API_KEY` — protects admin endpoints

## Disclaimer

Research software only. No real trading until paper results prove a durable edge.
