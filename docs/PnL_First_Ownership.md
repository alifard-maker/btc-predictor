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

### Milestone to unlock Phase 2

**20 consecutive live BTC hourly intervals** with `net_pnl_usd >= 0` (tracked by `scripts/pnl_first_milestone_tracker.py`).

When achieved: notify owner, auto-proceed to Phase 2 (paper-validated ETH S1, Kalshi tape backtest infra).

## Phase 2 (after milestone)

- ETH S1 live (same profile)
- Historical ask-side backtest pipeline
- Paper simulator v2 (fees + fill probability)
- Hard bucket bans from live shadow ledger

## Phase 3+

- S2 only with path/reachability model
- Scale caps after 2 weeks positive BTC+ETH
- 15m as separate product

## Ops

- Live monitor: `scripts/run_2h_manager_watchdog.py` (30s delta cycles)
- Milestone: `python scripts/pnl_first_milestone_tracker.py`
- Profile: `hourly.bot.live_mechanics_profile: pnl_first`
