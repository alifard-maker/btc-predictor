# 24/7 paper bots on Railway

Bots run **server-side** in the API process (APScheduler). The dashboard is only for settings and logs — **Auto-bet does not require an open browser**.

## What persists across deploys

| Data | Path (with volume) | Reset only via |
|------|-------------------|----------------|
| Paper bankroll | `data/logs/hourly_bot_*.db`, `slot15_bot_*.db` | Dashboard **Reset paper bankroll** |
| Trade logs | Same SQLite files | Never (unless volume deleted) |
| Auto-bet toggle | `bot_settings` in same DBs | Dashboard toggle |
| Candles / models | `data/candles/`, `data/models/` | Manual delete |

**Without a Railway volume**, the container filesystem is wiped on every redeploy — bankroll, logs, and bot settings reset to defaults.

## Setup (one time)

### 1. Attach a persistent volume

1. Railway project → your **btc-predictor** service → **Volumes**
2. **Add volume** → mount path: **`/data`**
3. Redeploy

The Dockerfile sets `DATA_DIR=/data`. All bot DBs, candles, and models live under that path.

### 2. Required env vars

| Variable | Value |
|----------|--------|
| `DATA_DIR` | `/data` (default in Dockerfile) |
| `ENABLE_SCHEDULER` | `true` |
| `APP_PASSWORD` | Dashboard login |
| `ADMIN_API_KEY` | Admin API |

Optional Postgres: `DATABASE_URL` (prediction calibration; bot paper state stays in SQLite under `/data`).

### 3. Start paper auto-bet

**Option A — dashboard (recommended)**  
Open `/dashboard` → enable **Auto-bet** + **Paper** on the bots you want. Settings are saved to SQLite on the volume and survive redeploys.

**Option B — env bootstrap (fresh deploy only)**  
On first boot with an empty bot DB (no trades yet):

```bash
PAPER_BOT_AUTO_ENABLE=btc,eth,slot15
```

Tokens: `btc`, `eth`, `slot15`, `btc-hourly`, `eth-hourly`, `btc-slot15`, `eth-slot15`, or `all`.  
Does **not** re-enable after you turn Auto-bet off if trades already exist.

### 4. Verify server is running

- Dashboard bot panel: **Server running** + **Last bot cycle** (&lt;30s when healthy)
- `GET /health` → `scheduler_running: true`, `data_dir: "/data"`

## Deploy behavior

- **Redeploy / code update**: bankroll + logs **kept** if volume mounted at `/data`
- **Never** resets paper bankroll on startup (migrations are additive only)
- Reset bankroll only via dashboard button or explicit API `POST /api/.../bot/reset-bankroll`

## Architecture

```
Railway container
├── uvicorn (FastAPI)
├── APScheduler (background thread)
│   ├── run_hourly_bot_continuous  (every ~5s)
│   ├── run_slot15_bot_continuous  (every ~5s)
│   └── predictions, fetch, resolve…
└── /data  (Railway volume)
    └── logs/
        ├── hourly_bot_btc.db
        ├── hourly_bot_eth.db
        ├── slot15_bot_btc.db
        └── slot15_bot_eth.db
```

Browser closed → scheduler keeps polling → paper trades logged → reopen dashboard anytime.
