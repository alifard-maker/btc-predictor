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

## Deploy to Vercel

1. Push to GitHub (included in `btc-predictor` repo under `dashboard/`)
2. [vercel.com](https://vercel.com) → New Project → import repo
3. Set **Root Directory** to `dashboard`
4. Add env var: `NEXT_PUBLIC_API_URL=https://btc-predictor-production-f460.up.railway.app`
5. Deploy

## Screens

- **Live prediction** — UP/DOWN %, signal, price, expected move
- **System status** — exchange, scheduler, candle count
- **Calibration** — predicted vs actual chart
- **History** — past predictions and outcomes
