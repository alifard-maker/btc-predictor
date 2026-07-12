# Kalshi Crypto Desk — Pitch Brief

**Probabilistic auto-trading for Kalshi BTC/ETH and index hourly/15m contracts**

*Version Beta 4.0.60 · Self-hosted · ML-first · Settlement-native*

---

## One-liner

We built a **quant trading stack** for Kalshi crypto and index hourly/15-minute markets: LightGBM models estimate fair probability, execution only fires when **ask-edge beats fees**, and a full ops dashboard runs paper, live, and A/B trials side by side. It is not a generic rule bot — it is a **prop desk in a box** for contracts that settle on BRTI/ERTI.

---

## The problem

Kalshi crypto and index products (hourly thresholds, range bands, 15m slots) move fast and settle on **official index references**, not exchange spot. Most traders either trade manually and miss entries, use no-code rule bots with no model of fair value, or spend months building Python, Kalshi API integration, ML, and dashboards from scratch.

There is a gap between **automation** and **edge**.

---

## Our solution

An end-to-end system:

| Layer | What it does |
|--------|----------------|
| **Predict** | LightGBM on OHLCV + structure features; daily retrain + 6h isotonic recalibration |
| **Map** | Model probability → Kalshi contract picks (threshold, range, 15m slot) |
| **Gate** | Fee-adjusted ask-edge, regime filters, Kelly sizing, correlation guards, cooldowns |
| **Execute** | Paper or live via Kalshi API; cross-spread on high edge; optional bracket orders |
| **Learn** | Adaptive bucket pause/probe from trade P&L; auto-tune on passive bots |
| **Report** | Dashboard: P&L, skip reasons, live vs paper/trial compare, reconcile health |

**Six independent bots today:** BTC hourly, BTC hourly trial, BTC 15m, ETH hourly, ETH hourly trial, ETH 15m — plus SPX/NDX hourly experiments. Each has separate state, risk cap, and settings.

---

## How we compare to Bot for Kalshi

Bot for Kalshi ($99/mo) is a **broad no-code platform**: visual step builder, any market, encrypted multi-user keys, templates (mean reversion, momentum, settlement yield). Excellent for **general Kalshi automation**.

**We are purpose-built for probability mispricing on index-settled crypto and index products.**

| Dimension | Bot for Kalshi | Our stack |
|-----------|----------------|-----------|
| **Core edge** | Rules you define (price, time, triggers) | **ML probability vs market ask** |
| **Markets** | All Kalshi | **BTC/ETH hourly + 15m** (+ index hourly) |
| **Settlement** | You configure | **BRTI/ERTI gates**, hour/slot rollover force-close |
| **Research loop** | In-app template backtests | **Paper + live + trial bots**, performance reports |
| **Execution depth** | Brackets, stops, limits | Ask-edge, cross-spread, whipsaw guard, range caps |
| **Who runs it** | They host | **You host** (Railway); full control, no platform rent |
| **Multi-user** | Built-in | Roadmap (separate deploys today; in-app tenants later) |

**Analogy:** Bot for Kalshi is **Zapier for Kalshi**. We are **a small quant fund's infra** for one product vertical.

---

## Why this matters on Kalshi

- **Contracts are binary and fee-sensitive** — edge is measured in cents vs model prob, not vibes.
- **Settlement index ≠ exchange spot** — live entries require BRTI/ERTI; we enforce that.
- **Hourly churn is brutal** — exits-before-entries, hybrid/adaptive TP, cut-loss cooldowns, hour-cap discipline.
- **Paper lies if execution differs** — live vs paper/trial compare catches fill drift (resting limits vs cross-spread).

---

## Traction and proof (honest stage)

- **Live trading** on personal Kalshi account with versioned deploys (Beta 4.0.x).
- **Paper and trial bots** in parallel for controlled experiments (leg-stop trial vs thesis hourly).
- **Operational maturity:** WAL SQLite, 429 circuit breaker, live reconcile, DB backups, settlement cleanup.
- **Not claiming** audited track record or guaranteed returns — research software maturing toward durable edge.

**Go-live checklist:** 20+ hours flat/positive hour P&L, reconcile stable, TAKE PROFIT dominates cuts, no phantom settlement rows.

---

## Business model options

| Path | Description |
|------|-------------|
| **Personal alpha** | Keep private; compound on own account |
| **Managed accounts** | Separate Railway + Kalshi key per client (works today) |
| **SaaS (future)** | Multi-tenant users, encrypted keys, shared strategy deploys |
| **Licensing** | Stack for sophisticated Kalshi traders who want ML + ops |

Infrastructure: ~$5–20/mo hosting vs $99/mo generic bot platforms — you own maintenance.

---

## Competitive moat (if productized)

- **Domain-specific ML pipeline** — 15m, hourly, 2nd-chance models with production calibration
- **Execution rule library** — tuned on real Kalshi friction, not replicable in a visual builder quickly
- **Compare infrastructure** — paper/trial/live on same clock = faster iteration
- **Index product focus** — depth on KXBTCD, KXETH15M vs shallow coverage across all markets

---

## The ask

**Collaborators:** Positioning feedback, benchmark design (us vs Bot for Kalshi on same caps), ops help.

**Investors:** Capital for productization (multi-user, encrypted keys, onboarding) and longer live track record — not for guaranteed bot returns.

**Early users (future):** Hosted seats: same bots, your Kalshi key, strategy updates on every deploy.

---

## Closing line

**Bot for Kalshi helps you automate what you already believe. We help you discover and trade what the model believes — with the guardrails a Kalshi crypto desk actually needs.**

---

*Disclaimer: Prediction markets involve risk of loss. Past paper or live results do not guarantee future performance. Not financial advice.*
