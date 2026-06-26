# BTC Predictor Dashboard

Next.js frontend for the Railway BTC predictor API.

## Run locally

```bash
cd dashboard
cp .env.local.example .env.local
npm install
npm run dev
```

Open http://localhost:3000

## Deploy to Railway (same as your WC app)

Use the **same Railway project** as btc-predictor backend.

### Option A — New service from same repo (recommended)

1. Railway project → **+ New** → **GitHub Repo**
2. Select **`alifard-maker/btc-predictor`** (same repo as backend)
3. Click the new service → **Settings** → **Root Directory** → set to `dashboard`
4. **Variables** → add:
   ```
   NEXT_PUBLIC_API_URL=https://btc-predictor-production-f460.up.railway.app
   ```
5. **Networking** → **Generate Domain** → you get e.g. `btc-dashboard-production-xxxx.up.railway.app`

Railway uses `dashboard/Dockerfile` automatically.

### Option B — If Railway doesn't show Root Directory

Set these in the service **Variables**:

| Variable | Value |
|----------|-------|
| `RAILWAY_DOCKERFILE_PATH` | `dashboard/Dockerfile` |
| `NEXT_PUBLIC_API_URL` | `https://btc-predictor-production-f460.up.railway.app` |

Or set **Settings → Build → Dockerfile Path** to `dashboard/Dockerfile`.

### Your project layout (like WC app)

```
Railway Project
├── Postgres          ← database
├── web               ← your WC soccer app
├── btc-predictor     ← Python API backend
└── btc-dashboard     ← this Next.js UI (new)
```

## Screens

- **Live prediction** — UP/DOWN %, signal, price, expected move
- **System status** — exchange, scheduler, candle count
- **Calibration** — predicted vs actual chart
- **History** — past predictions and outcomes
