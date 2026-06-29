# 24/7 paper bots on Railway

Bots run **server-side** in the API process (APScheduler). The dashboard is only for settings and logs — **Auto-bet does not require an open browser**.

## What persists across deploys

| Data | Path (with volume) | Reset only via |
|------|-------------------|----------------|
| Paper bankroll | `/data/logs/hourly_bot_*.db`, `slot15_bot_*.db` | Dashboard **Reset paper bankroll** |
| Trade logs | Same SQLite files | Never (unless volume deleted) |
| **Backups** | `/data/backups/paper/`, `/data/backups/live/` | Manual delete |
| Auto-bet toggle | `bot_settings` in same DBs | Dashboard toggle |
| Candles / models | `/data/candles/`, `/data/models/` | Manual delete |

**Without a Railway volume**, the container filesystem is wiped on every redeploy — bankroll, logs, and bot settings reset to defaults. The dashboard shows a red warning when this is the case.

## Setup (one time)

### 1. Attach a persistent volume (required)

**CLI (from your Mac):**

```bash
railway login
cd btc-predictor
./scripts/railway-add-data-volume.sh
```

**Or manually:**

1. Railway project → **btc-predictor** service → **Volumes**
2. **Add volume** → mount path: **`/data`**
3. **Redeploy** (critical — volume attaches on next deploy)

The Dockerfile sets `DATA_DIR=/data`. All bot DBs, backups, candles, and models live under that path.

Verify after redeploy:

```bash
curl -s https://YOUR-DOMAIN.up.railway.app/health | jq '{volume_mounted_at_data, data_dir, log_backup}'
```

`volume_mounted_at_data` must be **`true`**.

### 2. Automatic log backups

Enabled by default (`log_backup` in `config.yaml`):

| Path | Contents |
|------|----------|
| `/data/backups/paper/` | Paper trade CSVs + `audit_trades.jsonl` |
| `/data/backups/live/` | **Live trades for tax** — CSVs + append-only audit log |
| `/data/backups/snapshots/` | Timestamped full DB copies (90-day retention) |

- Runs every **15 minutes** and on **startup**
- Each live/paper trade is appended to the mode-specific audit log immediately
- Manual trigger: `POST /api/admin/backup-logs` with `X-Api-Key`

```bash
curl -X POST -H "X-Api-Key: YOUR_ADMIN_KEY" https://YOUR-DOMAIN/api/admin/backup-logs
```

Local CLI: `PYTHONPATH=. python scripts/backup_logs.py`

### 3. Required env vars

| Variable | Value |
|----------|--------|
| `DATA_DIR` | `/data` (default in Dockerfile) |
| `ENABLE_SCHEDULER` | `true` |
| `APP_PASSWORD` | Dashboard login |
| `ADMIN_API_KEY` | Admin API |

Optional Postgres: `DATABASE_URL` (prediction calibration; bot paper state stays in SQLite under `/data`).

### 4. Start paper auto-bet

**Option A — dashboard (recommended)**  
Open `/dashboard` → enable **Auto-bet** + **Paper** on the bots you want. Settings are saved to SQLite on the volume and survive redeploys.

**Option B — env bootstrap (fresh deploy only)**  
On first boot with an empty bot DB (no trades yet):

```bash
PAPER_BOT_AUTO_ENABLE=btc,eth,slot15
```

Tokens: `btc`, `eth`, `slot15`, `btc-hourly`, `eth-hourly`, `btc-slot15`, `eth-slot15`, or `all`.  
Does **not** re-enable after you turn Auto-bet off if trades already exist.

### 5. Verify server is running

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
