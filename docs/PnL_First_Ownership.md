# P&L-First Ownership Plan

Baseline tagged **`research-platform-1.0`** — full research platform (multi-bot, S1+S2, trials, dashboard).

## Mission

**Positive live expectancy first, scale second.** Unlimited budget goes to measurement and execution quality, not more marginal legs.

## Phase 0–1 (active now) — `pnl_first` live profile

| Gate | Setting |
|------|---------|
| Markets | **BTC hourly S1 threshold only** — S2 range blocked |
| Tail entries | Blocked (≤20¢) |
| Concurrency | Max **2** open legs, **1** entry/cycle |
| Scale-in | Off |
| Adaptive / rally overlays | Off |
| Min ask-edge | **15¢** |
| Execution | **Taker-only** (cross at ask when edge ≥15¢; no passive rests) |
| Live EV | Enter only if fee-adjusted EV/contract > floor |
| Regime | **Enforced** even in FREE dashboard mode |
| ETH live | **Off** until BTC milestone hit |
| Daily loss cap | $50/bot (existing `bot_risk`) |

### Milestone to unlock Phase 2 (pipeline proof)

**20 consecutive live BTC hourly hours** where the full gate stack was exercised under `pnl_first` — **not** PnL-gated fills.

| Counts toward streak | Requirement |
|----------------------|-------------|
| Per hour | BTC hourly **live**, bot **cycled**, manager **preflight OK** |
| Session (once) | All gate families seen: **regime**, **edge**, **taker**, **preflight** |

Tracked in manager state (`pipeline_milestone`) and exposed at `GET /api/pnl-first/manager` → `milestone_now`.

When achieved: notify owner, proceed to Phase 2 (paper-validated ETH S1, Kalshi tape backtest infra).

### Engineering rule (Jul 2026)

**Backtest before believing gate/fill-rate claims.** Synthetic 3y replay + production skip logs — not intuition. Example: `pnl_first` taker-only required cross-spread enabled; passive preset disabled it → zero fills until fixed (5.0.9). Reproduce: `scripts/pnl_first_gate_frequency_3y.py` (gate pass rates), `scripts/backtest_hourly_deploy_compare.py --profiles pnl_first` (P&L). Artifacts: `data/logs/pnl_first_gate_frequency_3y.json`, `data/logs/backtest_pnl_first_3y.json`.

## Phase 2 (after milestone)

- ETH S1 live (same profile)
- Historical ask-side backtest pipeline
- Paper simulator v2 (fees + fill probability)
- Hard bucket bans from live shadow ledger

### Deferred: Phase 1.5 execution — Kalshi WebSocket orderbook (Jul 2026)

**Decision:** Do **not** implement during Phase 0–1 pipeline proof. Revisit when triggers below fire.

**Why defer:** Current profile is selective taker (~1–4 fills/hour, $15/h cap). Dominant risks are model/gate validity and overfitting, not sub-second HFT latency. REST + snapshot quotes are adequate for debug; leg-stop slippage (~$0.15/hour observed) is worth fixing later, not before walk-forward ML + v3 structure results.

**Implement when any of:**

| Trigger | Threshold |
|---------|-----------|
| Live scale | Spend cap above $15/h or passive/resting entries enabled |
| Fill drift | Kalshi vs bot hour P&L drift > $0.50 on **3+** hours in a week |
| Stop quality | Leg-stop exits consistently worse than book-implied ask by >3¢ |
| Phase 2 start | ETH live or paper sim v2 needs book-aware fill model |

**Scope (when built):**

1. `orderbook_delta` WebSocket on active hour strikes only (official Kalshi WS v2)
2. Pre-trade: recompute ask-edge from live book before `create_order`
3. Stops: book-aware limit/cross vs blind taker
4. Keep REST for positions, fills, reconciliation

**Wire points:** `src/data/kalshi.py` (WS client), `live_entry_price.py`, leg-exit path in hourly bot.

### Deferred: Track B — terminal microstructure (final 15m) (Jul 2026)

**Decision:** Run **parallel to Track A** (hourly ML + gates), not as a replacement during Phase 0–1. Track A proves pipeline + hourly signal; Track B tests whether **gamma / book lag / spot-led fair value** in the last 15 minutes is a separate, viable edge.

**Thesis (owner, Jul 2026):** Hourly binaries are not monthly forecasts — they are short-dated options with high gamma near strike at expiry. Kalshi's book can lag rapid spot (BRTI) moves; ML may help predict localized momentum or mean-reversion in the terminal window. Cross-venue lag (Kalshi vs Polymarket) and execution latency are real but are **different products** from current `pnl_first` hourly entries.

**Track A vs Track B:**

| | Track A (active) | Track B (deferred research) |
|--|------------------|----------------------------|
| Horizon | Full hour, hour-open bias | Final **15 minutes** only |
| Model | LightGBM on 1h bars + structure | Tick/1m features; short-horizon drift or vol |
| Entries | Blocked when `hours_to_settle < 0.25` | **Only** when `hours_to_settle < 0.25` |
| Execution | REST, taker-only | Requires Phase 1.5 WS + fast spot feed |
| Goal | Pipeline proof + walk-forward hourly edge | Spot-implied prob vs Kalshi mispricing |

**Do not merge** Track B signals into Track A entries until shadow data supports it.

#### Phase B0: shadow logger (paper only, no orders)

**Purpose:** Measure whether spot-led fair value systematically leads Kalshi mids in the terminal window — zero capital risk, no toxic passive flow.

**Implement when:** Track A milestone in progress (can run in parallel on Railway; no live orders).

**Spec:**

| Item | Detail |
|------|--------|
| **Window** | Last 15m of each BTC hourly event (`hours_to_settle` 0.25 → 0) |
| **Cadence** | Every **10s** (match `poll_seconds`) |
| **Spot** | BRTI live (existing Kalshi cfbenchmarks passthrough) |
| **Book** | Kalshi REST mid/ask per active strike (upgrade to WS when Phase 1.5 fires) |
| **Fair value** | `P(close above strike \| spot, time_left, realized_vol)` — simple GBM or empirical terminal vol |
| **Logged fields** | `ts`, `event_ticker`, `strike`, `spot`, `time_left_s`, `spot_implied_prob`, `kalshi_yes_mid`, `kalshi_yes_ask`, `edge_cents`, `moneyness_usd`, `realized_vol_1h` |
| **Output** | Append JSONL: `/data/logs/terminal_shadow/{event_ticker}.jsonl` |
| **Settlement join** | After hour close, append `settled_yes`, `shadow_would_pnl` (hypothetical taker at logged ask, 1 contract, fees) |
| **No orders** | Logger only; `pnl_first` entry blocks remain unchanged |

**Promotion triggers (shadow → paper trades → live):**

| Trigger | Threshold |
|---------|-----------|
| Persistent edge | Median `edge_cents` ≥ **8¢** after fees in final 10m, **50+** hourly events |
| Spot leads book | Spot-implied prob moves **before** Kalshi mid on **≥60%** of large spot shocks (>0.15% in 60s) |
| Hypothetical PnL | Shadow `would_pnl` positive over **100+** terminal windows with stable $/trade |
| Track A plateau | Walk-forward hourly edge ≤ 0 after v3 + unified WF, **or** owner opts in early |

**Full Track B scope (after shadow promotes):**

1. Terminal-only bot profile (`terminal_microstructure`) — invert `min_hours_to_settle` gate (max 0.25, not min)
2. Phase 1.5 Kalshi WS + sub-2s spot-to-order path (may need co-located runner if Railway too slow)
3. ML: gradient boosting on 1m/ tick aggregates (momentum, vol compression, distance-to-strike); LSTM optional
4. **Taker-only** in gamma window — no passive limits (toxic flow risk)
5. **Polymarket scanner** — separate workstream; verify settlement alignment before calling arb; paper-first on both venues

**Risks to respect:**

- **Toxic flow:** passive quotes in final 15m get sniped on spot jumps — stay taker or IOC
- **Latency:** 150ms vs 15ms loses on the same signal — measure Railway round-trip before live
- **Slippage:** 2¢ on a 15¢ edge erodes EV — shadow must use **ask**, not mid
- **Overfitting:** terminal patterns are noisy — walk-forward by week, not one lucky hour

**Wire points (shadow):** new `src/trading/terminal_shadow_logger.py`, hook from `HourlyBot.run_continuous_cycle` or scheduler when `hours_to_settle ≤ 0.25`; reuse `src/data/kalshi.py` BRTI + strike picks from hourly tab.

**Wire points (live, later):** `terminal_microstructure` profile in `mechanics_profiles.py`, `live_entry_price.py`, Phase 1.5 WS client.

### Trade timing analytics (Jul 2026)

Track **when in the hour** live legs make or lose money — minutes remaining until settlement at **entry** and **exit**.

| Surface | Location |
|---------|----------|
| Module | `src/trading/trade_timing_analytics.py` |
| Manager API | `GET /api/pnl-first/manager` → `trade_timing` |
| Manager log | `/data/logs/pnl_first_manager/status.json` (hourly with milestone) |
| Performance report | `bot_performance_report` → `trade_timing` |
| Entry log field | `entry_settings.minutes_to_settle` on new enters |

Buckets: `0–5m`, `5–10m`, `10–15m`, `15–30m`, `30–45m`, `45–60m`, `60m+` left. Informs Track B (terminal window) vs Track A (hour-open) without changing gates yet.

## Phase 3+

- S2 only with path/reachability model
- Scale caps after 2 weeks positive BTC+ETH
- 15m as separate product

## Ops

- **Railway manager** (24/7): `pnl_first_manager` in config — runs inside APScheduler every 30s on production. Sleep lock, preflight, milestone tracking. Status: `GET /api/pnl-first/manager` or `/health` → `pnl_first_manager`.
- **Local monitor** (optional while Mac is on): `scripts/run_2h_manager_watchdog.py`
- Milestone: `scripts/pnl_first_milestone_tracker.py` or Railway manager state at `/data/logs/pnl_first_manager/status.json`
- Profile: `hourly.bot.live_mechanics_profile: pnl_first`

### Owner POA (Jul 5 2026)

If pinged before live wake and **no reply**, manager may exercise **power of attorney**:

- Only after **fill pairing + preflight green**
- **BTC hourly S1-only** under `pnl_first` gates (not research spray)
- Cap **`owner_poa_debug_cap_usd`** ($15/h default) for debug/proof sessions
- ETH stays off; no S2/tails/scale-in
- Set `poa_exercise_requested: true` in manager state (not automatic on config alone)
- Document actions in `/data/logs/pnl_first_manager/status.json`

```bash
# After ping timeout — exercise POA on Railway volume
python scripts/pnl_first_poa_exercise.py --reason "ping_unanswered"
```

### Flip live switch (Railway)

1. Manager fixes Kalshi pairing + reconcile preflight green
2. Set `pnl_first_manager.trading_armed: true` (or `auto_wake_when_ready: true`) in config + redeploy
3. Manager wakes BTC at next clean hour when preflight passes
4. **20 consecutive green live hours** → Phase 2 auto
