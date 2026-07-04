"""V3 pressing-mode mechanics backtest — hour momentum governor on V1 momentum μ."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.backtest.fill_simulator import FillSimulator, OrderStyle
from src.backtest.fee_model import FeeModel
from src.backtest.hourly_mechanics_backtest import (
  HourSimState,
  MuMode,
  SimLeg,
  _blended_mu_for_poll,
  _brti_poll_prices,
  _contract_yes_won,
  _mark_leg,
  _pick_from_market,
  _summarize_reason,
  _synthetic_markets,
  pick_edge_ok,
)
from src.backtest.mechanics_profiles import MechanicsProfile, apply_mechanics_profile, replay_entry_pricing
from src.backtest.metrics import max_drawdown
from src.trading.bot_entry_presets import effective_bot_entry_strategy
from src.trading.bot_live_exit import (
  allow_live_cut_loss,
  apply_live_exit_entry_guards,
  effective_live_take_profit_usd,
  live_exit_config,
)
from src.trading.entry_strategy import (
  CycleEntryBudget,
  correlation_block_reason,
  entry_budget_usd,
  passes_ask_edge_gate,
  passes_tail_entry_gate,
  rank_hourly_candidates,
)
from src.trading.hour_momentum import (
  HourMomentumContext,
  HourMomentumPolicy,
  HourMomentumState,
  apply_hour_momentum_policy,
  compute_hour_momentum,
  resolve_late_entry_config,
)
from src.trading.hourly_regime import (
  LateEntryConfig,
  entry_pick_settle_skip_reason,
  is_late_entry_path,
  late_entry_config,
)
from src.trading.live_entry_price import resolve_live_entry_price
from src.trading.live_inventory_guards import apply_live_inventory_guards
from src.trading.live_regime_adaptive import (
  adaptive_defense_entry_block_reason,
  adaptive_live_entry_pricing,
  adaptive_range_band_block_reason,
  apply_adaptive_passive_guards,
  assess_adaptive_passive_mode,
  cross_spread_allowed_for_adaptive,
  defense_entries_blocked,
)

PressingVariant = Literal["baseline_a", "baseline_b_static", "v3_pressing"]

VARIANT_LABELS: dict[str, str] = {
  "baseline_a": "Baseline A — current mechanics replay (no momentum, no late-entry path)",
  "baseline_b_static": "Baseline B — static normal tier (4 entries, late entry 15¢, no governor)",
  "v3_pressing": "V3 — full hour momentum governor + late-entry tiers",
}


def _poll_prices_with_late_window(open_px: float, high: float, low: float, close: float) -> list[float]:
  """Four standard polls plus a late-hour poll (~12 min to settle)."""
  base = _brti_poll_prices(open_px, high, low, close)
  late = low + 0.82 * (high - low)
  return [*base, late]


def _hours_left_for_poll(poll_idx: int) -> float:
  return max(0.08, 1.0 - poll_idx * 0.22)


@dataclass
class HourSimStateExt(HourSimState):
  momentum_states: dict[str, int] = field(default_factory=dict)
  late_entry_fills: int = 0


def _record_momentum_state(state: HourSimStateExt, policy: HourMomentumPolicy | None) -> None:
  if policy is None:
    key = "disabled"
  else:
    key = policy.state.value
  state.momentum_states[key] = state.momentum_states.get(key, 0) + 1


def _unrealized_total(state: HourSimState, picks_cache: dict[str, dict[str, Any]]) -> float:
  total = 0.0
  for leg in state.legs:
    pick = picks_cache.get(leg.ticker) or {}
    _, unreal = _mark_leg(leg, pick)
    total += unreal
  return total


def _static_normal_late_entry(cfg: dict[str, Any]) -> LateEntryConfig:
  """Baseline B: fixed normal-tier late entry (15¢) without momentum governor."""
  base = late_entry_config(cfg)
  return LateEntryConfig(
    enabled=base.enabled,
    min_hours=base.min_hours,
    min_ask_edge_cents=15.0,
    max_stake_usd=base.max_stake_usd,
  )


def simulate_hour_pressing(
  *,
  open_px: float,
  high: float,
  low: float,
  close: float,
  hour_open: float,
  momentum_4h_pct: float,
  cfg: dict[str, Any],
  profile: MechanicsProfile,
  max_spend: float,
  fills: FillSimulator,
  hour_ts: datetime,
  mu_mode: MuMode = "momentum",
  variant: PressingVariant = "v3_pressing",
) -> HourSimStateExt:
  cfg = apply_mechanics_profile(cfg, profile)
  state = HourSimStateExt()
  sigma = max(30.0, (high - low) * 0.6 + open_px * 0.001)
  markets = _synthetic_markets(open_px)
  settle = close

  estrat = effective_bot_entry_strategy(cfg, kind="hourly", aggressive=False, tuning=None)
  estrat = apply_live_inventory_guards(estrat, cfg, mode="live", kind="hourly")
  estrat = apply_live_exit_entry_guards(estrat, cfg, mode="live", kind="hourly")
  live_exit = live_exit_config(cfg, kind="hourly")
  base_pricing = replay_entry_pricing(cfg, profile=profile, aggressive=False, kind="hourly")

  use_late_poll = variant in ("baseline_b_static", "v3_pressing")
  polls = (
    _poll_prices_with_late_window(open_px, high, low, close)
    if use_late_poll
    else _brti_poll_prices(open_px, high, low, close)
  )
  picks_cache: dict[str, dict[str, Any]] = {}

  for i, brti in enumerate(polls):
    hours_left = _hours_left_for_poll(i)
    terminal_mu = _blended_mu_for_poll(
      mu_mode=mu_mode,
      open_px=open_px,
      high=high,
      low=low,
      close=close,
      hour_open=hour_open,
      momentum_4h_pct=momentum_4h_pct,
      poll_idx=min(i, 3),
      poll_prices=polls[:4],
      brti=brti,
      hour_ts=hour_ts,
      sigma=sigma,
      cfg=cfg,
    )
    model_base = 0.5 + 0.5 * math.tanh((terminal_mu - hour_open) / sigma)
    candidates: list[tuple[float, dict, dict]] = []
    for m in markets:
      st = str(m.get("strike_type") or "")
      floor = m.get("floor_strike")
      if st == "greater" and floor is not None:
        dist = (float(floor) - terminal_mu) / sigma
        mp = max(0.03, min(0.97, 0.5 - 0.45 * math.tanh(dist)))
      else:
        mp = model_base
      pick = _pick_from_market(m, brti, sigma=sigma, model_prob=mp)
      picks_cache[pick["ticker"]] = pick
      if pick["signal"] in ("BUY YES", "BUY NO"):
        candidates.append((abs(float(pick["edge"])), pick, {}))

    for leg in list(state.legs):
      pick = picks_cache.get(leg.ticker) or {}
      mark, unreal = _mark_leg(leg, pick)
      hold_s = (i + 1) * 600
      pos = {
        "opened_at": (hour_ts - timedelta(seconds=3600 - hold_s)).isoformat(),
        "entry_price_cents": leg.entry_cents,
        "entry_source": "normal",
        "market_ticker": leg.ticker,
        "side": leg.side,
      }
      tp_usd = effective_live_take_profit_usd(pos, live_exit.take_profit_usd or 0.08, cfg, kind="hourly")
      exit_reason = None
      if unreal >= tp_usd and hold_s >= 60:
        exit_reason = "TAKE PROFIT"
      elif unreal <= -live_exit.cut_loss_min_usd and allow_live_cut_loss(
        exit_reason="CUT LOSSES",
        unrealized_usd=unreal,
        pos=pos,
        settings_min_hold=120,
        cfg=cfg,
        kind="hourly",
      ):
        exit_reason = "CUT LOSSES"

      if exit_reason:
        fee = FeeModel(cfg=cfg).leg_fee_usd(mark, leg.contracts, is_maker=False)
        pnl = unreal - fee
        state.realized_pnl += pnl
        state.cash_at_risk -= leg.cost_usd
        state.exits += 1
        if pnl > 0:
          state.wins += 1
        elif pnl < 0:
          state.losses += 1
        _summarize_reason(state, exit_reason, pnl)
        state.legs.remove(leg)

    expected_move = (terminal_mu - brti) / brti * 100.0 if brti else 0.0
    regime_allow = abs(momentum_4h_pct) >= 0.12 or abs(expected_move) >= 0.12
    ranked = rank_hourly_candidates([(e, p, {}) for e, p, _ in candidates], estrat=estrat)
    tab = {
      "live": {
        "current_price": brti,
        "expected_move_pct": expected_move,
        "terminal_mu": terminal_mu,
        "blended_mu": terminal_mu,
        "regime": {"allow_trade": regime_allow, "reasons": []},
        "primary_pick": ranked[0][3] if ranked else None,
      },
      "locked": {"reference_price": hour_open},
      "hour_open": {"reference_price": hour_open},
      "brti_live": brti,
      "intrahour_opportunity": {
        "highlight": (low < open_px * 0.998 and brti > low * 1.0015 and pick_edge_ok(ranked)),
      },
    }
    adaptive = assess_adaptive_passive_mode(
      tab=tab, cfg=cfg, realized_pnl_usd=state.realized_pnl, aggressive=False, mode="live",
    )
    if adaptive.mode == "locked" or defense_entries_blocked(adaptive, cfg):
      continue

    estrat_poll = apply_adaptive_passive_guards(estrat, adaptive, cfg)
    pricing = adaptive_live_entry_pricing(base_pricing, adaptive, cfg)

    unrealized_total = _unrealized_total(state, picks_cache)
    primary_edge = None
    if ranked:
      primary_edge = float(ranked[0][3].get("edge") or 0)

    momentum_policy: HourMomentumPolicy | None = None
    late_entry_effective: LateEntryConfig | None = None
    if variant == "baseline_a":
      late_entry_effective = None
    elif variant == "baseline_b_static":
      late_entry_effective = _static_normal_late_entry(cfg)
    else:
      momentum_ctx = HourMomentumContext(
        realized_pnl_usd=state.realized_pnl,
        unrealized_pnl_usd=unrealized_total,
        closed_wins=state.wins,
        closed_losses=state.losses,
        exit_count=state.exits,
        adaptive_mode=adaptive.mode,
        primary_pick_edge=primary_edge,
      )
      momentum_policy = compute_hour_momentum(momentum_ctx, cfg)
      _record_momentum_state(state, momentum_policy)
      estrat_poll = apply_hour_momentum_policy(estrat_poll, momentum_policy)
      late_entry_effective = resolve_late_entry_config(cfg, momentum_policy)

    cycle_budget = CycleEntryBudget(estrat_poll)
    for _c, _e, _s, pick, _bet in ranked[:cycle_budget.max_cycle_candidates()]:
      if not cycle_budget.can_enter(pick):
        continue
      if len(state.legs) >= estrat_poll.max_concurrent_positions:
        break
      if state.cash_at_risk >= max_spend:
        break
      side = "yes" if pick["signal"] == "BUY YES" else "no"

      if variant in ("baseline_b_static", "v3_pressing") and late_entry_effective is not None:
        skip = entry_pick_settle_skip_reason(
          hours_left, cfg, pick=pick, side=side, le_override=late_entry_effective,
        )
        if skip:
          continue
      elif not use_late_poll and hours_left < 0.25:
        continue

      if adaptive_range_band_block_reason(pick, adaptive, cfg):
        continue
      if adaptive_defense_entry_block_reason(pick, side, adaptive, cfg):
        continue
      block = correlation_block_reason(
        [{"side": l.side, "market_ticker": l.ticker} for l in state.legs],
        pick, side, resolve_pick=lambda t: picks_cache.get(t),
        ref_price=brti, estrat=estrat_poll,
      )
      if block:
        continue

      min_edge = estrat_poll.min_ask_edge_cents
      if (
        variant in ("baseline_b_static", "v3_pressing")
        and late_entry_effective is not None
        and is_late_entry_path(hours_left, pick, side, cfg, le_override=late_entry_effective)
      ):
        min_edge = late_entry_effective.min_ask_edge_cents

      ok, _ = passes_ask_edge_gate(pick, side, min_edge)
      if not ok:
        continue

      resolved = resolve_live_entry_price(pick, side, pricing=pricing, estrat=estrat_poll)
      if (
        resolved.get("execution_mode") == "cross_spread"
        and not cross_spread_allowed_for_adaptive(adaptive, cfg)
        and profile in ("current", "rally_only", "soft_rally")
      ):
        from dataclasses import replace as dc_replace

        resolved = resolve_live_entry_price(
          pick, side, pricing=dc_replace(pricing, cross_spread_enabled=False), estrat=estrat_poll,
        )
      price_cents = int(resolved["price_cents"])
      ok_tail, _, _ = passes_tail_entry_gate(pick, side, price_cents, estrat_poll)
      if not ok_tail:
        continue
      if state.resting_enter_count >= live_exit.max_resting_enters_per_hour:
        continue
      remaining = max_spend - state.cash_at_risk
      stake = entry_budget_usd(
        estrat=estrat_poll, bankroll_usd=max_spend, remaining_usd=remaining,
        pick=pick, side=side, entries_left=cycle_budget.entries_left(pick),
      )
      if (
        variant in ("baseline_b_static", "v3_pressing")
        and late_entry_effective is not None
        and is_late_entry_path(hours_left, pick, side, cfg, le_override=late_entry_effective)
      ):
        stake = min(stake, late_entry_effective.max_stake_usd)

      count = max(1, int(stake // (price_cents / 100.0))) if price_cents else 0
      cap = 6 if profile == "legacy" else (live_exit.max_adopted_contracts or 2)
      count = min(count, cap)
      if count <= 0:
        continue
      order_style = (
        OrderStyle.CROSS_SPREAD if resolved.get("execution_mode") == "cross_spread" else OrderStyle.PASSIVE_LIMIT
      )
      fill_res = fills.simulate_entry(
        prob_up=float(pick.get("kalshi_mid") or 0.5),
        side=side,
        order_style=order_style,
        time_to_settle_hours=hours_left,
        spread_cents=4,
      )
      state.resting_enter_count += 1
      if not fill_res.filled or fill_res.price_cents is None:
        state.resting_enters += 1
        continue
      fill_px = fill_res.price_cents
      cost = round(count * fill_px / 100.0, 2)
      state.legs.append(SimLeg(
        ticker=pick["ticker"], side=side, contracts=count,
        entry_cents=fill_px, cost_usd=cost,
        opened_at=hour_ts, label=str(pick.get("label") or ""),
      ))
      state.cash_at_risk += cost
      state.filled_enters += 1
      if (
        variant in ("baseline_b_static", "v3_pressing")
        and late_entry_effective is not None
        and is_late_entry_path(hours_left, pick, side, cfg, le_override=late_entry_effective)
      ):
        state.late_entry_fills += 1
      cycle_budget.record_entry(pick)

  for leg in list(state.legs):
    m = next(x for x in markets if x["ticker"] == leg.ticker)
    won = _contract_yes_won(settle, m)
    win = won if leg.side == "yes" else not won
    exit_c = 100 if win else 0
    if leg.side == "yes":
      pnl = (exit_c - leg.entry_cents) * leg.contracts / 100.0
    else:
      pnl = (leg.entry_cents - exit_c) * leg.contracts / 100.0
    state.realized_pnl += pnl
    state.exits += 1
    if pnl > 0:
      state.wins += 1
    elif pnl < 0:
      state.losses += 1
    _summarize_reason(state, "SETTLEMENT", pnl)
    state.legs.remove(leg)

  return state


def _summarize_variant_run(
  *,
  variant: PressingVariant,
  hourly_pnls: list[float],
  filled: int,
  resting: int,
  exits: int,
  wins: int,
  losses: int,
  hours_traded: int,
  win_hours: int,
  lose_hours: int,
  flat_hours: int,
  by_reason: dict[str, dict[str, float]],
  momentum_states: dict[str, int],
  late_entry_fills: int,
  n_hours: int,
  period_start: str,
  period_end: str,
  profile: str,
) -> dict[str, Any]:
  total_pnl = sum(hourly_pnls)
  equity = np.cumsum(hourly_pnls) if hourly_pnls else np.array([])
  closed = wins + losses
  for k in by_reason:
    by_reason[k]["pnl"] = round(by_reason[k]["pnl"], 2)

  return {
    "variant": variant,
    "label": VARIANT_LABELS[variant],
    "profile": profile,
    "mu_mode": "momentum",
    "hours_simulated": n_hours,
    "hours_with_fills": hours_traded,
    "period_start": period_start,
    "period_end": period_end,
    "total_pnl_usd": round(total_pnl, 2),
    "avg_pnl_per_hour_usd": round(total_pnl / n_hours, 4) if n_hours else 0.0,
    "max_drawdown_usd": round(max_drawdown(equity), 2) if len(equity) else 0.0,
    "filled_enters": filled,
    "resting_enters": resting,
    "late_entry_fills": late_entry_fills,
    "exits": exits,
    "wins": wins,
    "losses": losses,
    "win_rate": round(wins / closed, 4) if closed else 0.0,
    "winning_hours": win_hours,
    "losing_hours": lose_hours,
    "flat_hours": flat_hours,
    "by_exit_type": by_reason,
    "expectancy_per_fill_usd": round(total_pnl / filled, 4) if filled else 0.0,
    "momentum_state_polls": momentum_states,
  }


def run_pressing_variant_backtest(
  df_1h: pd.DataFrame,
  cfg: dict[str, Any],
  *,
  variant: PressingVariant,
  profile: MechanicsProfile = "current",
  max_spend: float = 15.0,
  warmup_bars: int = 24,
  mu_mode: MuMode = "momentum",
) -> dict[str, Any]:
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  fills = FillSimulator(app_cfg=cfg, fee_model=FeeModel(cfg=cfg))
  fills._rng = np.random.default_rng(42)

  hourly_pnls: list[float] = []
  filled = resting = exits = wins = losses = 0
  win_hours = lose_hours = flat_hours = hours_traded = 0
  by_reason: dict[str, dict[str, float]] = {}
  momentum_states: dict[str, int] = {}
  late_entry_fills = 0

  opens = df["open"].astype(float).values
  highs = df["high"].astype(float).values
  lows = df["low"].astype(float).values
  closes = df["close"].astype(float).values
  ts = pd.to_datetime(df["timestamp"], utc=True)

  for i in range(warmup_bars, len(df)):
    open_px = float(opens[i])
    mom_idx = max(0, i - 4)
    mom = (open_px - float(opens[mom_idx])) / float(opens[mom_idx]) * 100.0 if opens[mom_idx] else 0.0
    hour_ts = ts.iloc[i].to_pydatetime()
    st = simulate_hour_pressing(
      open_px=open_px,
      high=float(highs[i]),
      low=float(lows[i]),
      close=float(closes[i]),
      hour_open=open_px,
      momentum_4h_pct=mom,
      cfg=cfg,
      profile=profile,
      max_spend=max_spend,
      fills=fills,
      hour_ts=hour_ts,
      mu_mode=mu_mode,
      variant=variant,
    )
    hourly_pnls.append(st.realized_pnl)
    filled += st.filled_enters
    resting += st.resting_enters
    exits += st.exits
    wins += st.wins
    losses += st.losses
    late_entry_fills += st.late_entry_fills
    if st.filled_enters > 0:
      hours_traded += 1
    if st.realized_pnl > 0.01:
      win_hours += 1
    elif st.realized_pnl < -0.01:
      lose_hours += 1
    else:
      flat_hours += 1
    for k, v in st.by_reason.items():
      bucket = by_reason.setdefault(k, {"n": 0, "pnl": 0.0})
      bucket["n"] += int(v["n"])
      bucket["pnl"] += float(v["pnl"])
    for k, v in st.momentum_states.items():
      momentum_states[k] = momentum_states.get(k, 0) + v

  n_hours = len(df) - warmup_bars
  return _summarize_variant_run(
    variant=variant,
    hourly_pnls=hourly_pnls,
    filled=filled,
    resting=resting,
    exits=exits,
    wins=wins,
    losses=losses,
    hours_traded=hours_traded,
    win_hours=win_hours,
    lose_hours=lose_hours,
    flat_hours=flat_hours,
    by_reason=by_reason,
    momentum_states=momentum_states,
    late_entry_fills=late_entry_fills,
    n_hours=n_hours,
    period_start=str(ts.iloc[warmup_bars]),
    period_end=str(ts.iloc[-1]),
    profile=profile,
  )


def _split_holdout(df: pd.DataFrame, *, holdout_frac: float = 0.30, warmup_bars: int = 24) -> tuple[pd.DataFrame, pd.DataFrame]:
  df = df.sort_values("timestamp").reset_index(drop=True)
  n = len(df)
  split_idx = max(warmup_bars + 1, int(n * (1.0 - holdout_frac)))
  return df.iloc[:split_idx].reset_index(drop=True), df.iloc[split_idx - warmup_bars:].reset_index(drop=True)


def run_v3_pressing_comparison(
  cfg: dict[str, Any],
  df_1h: pd.DataFrame,
  *,
  years: float = 3.0,
  holdout_frac: float = 0.30,
  profile: MechanicsProfile = "current",
  max_spend: float = 15.0,
  warmup_bars: int = 24,
  variants: tuple[PressingVariant, ...] = ("baseline_a", "baseline_b_static", "v3_pressing"),
) -> dict[str, Any]:
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  if years > 0:
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=int(years * 365.25))
    df = df[df["timestamp"] >= start].reset_index(drop=True)

  df_in, df_hold = _split_holdout(df, holdout_frac=holdout_frac, warmup_bars=warmup_bars)

  full: dict[str, Any] = {}
  in_sample: dict[str, Any] = {}
  holdout: dict[str, Any] = {}

  for variant in variants:
    full[variant] = run_pressing_variant_backtest(
      df, cfg, variant=variant, profile=profile, max_spend=max_spend, warmup_bars=warmup_bars,
    )
    in_sample[variant] = run_pressing_variant_backtest(
      df_in, cfg, variant=variant, profile=profile, max_spend=max_spend, warmup_bars=warmup_bars,
    )
    holdout[variant] = run_pressing_variant_backtest(
      df_hold, cfg, variant=variant, profile=profile, max_spend=max_spend, warmup_bars=warmup_bars,
    )

  return {
    "bars": len(df),
    "period_start": str(df["timestamp"].min()) if len(df) else None,
    "period_end": str(df["timestamp"].max()) if len(df) else None,
    "holdout_frac": holdout_frac,
    "in_sample_bars": len(df_in) - warmup_bars,
    "holdout_bars": len(df_hold) - warmup_bars,
    "disclaimer": (
      "Synthetic Kalshi books + 1h OHLC with 5 intrahour polls (incl. late window). "
      "V1 momentum μ only. Not historical Kalshi contract prices."
    ),
    "full_period": full,
    "in_sample": in_sample,
    "holdout": holdout,
    "deltas_vs_baseline_a": {
      variant: {
        "full_pnl_usd": round(full[variant]["total_pnl_usd"] - full["baseline_a"]["total_pnl_usd"], 2),
        "holdout_pnl_usd": round(holdout[variant]["total_pnl_usd"] - holdout["baseline_a"]["total_pnl_usd"], 2),
      }
      for variant in variants
      if variant != "baseline_a"
    },
  }


def run_pressing_threshold_grid(
  cfg: dict[str, Any],
  df_1h: pd.DataFrame,
  *,
  years: float = 3.0,
  holdout_frac: float = 0.30,
  conservative_edges: tuple[float, ...] = (16.0, 18.0, 20.0),
  pressing_edges: tuple[float, ...] = (10.0, 12.0, 14.0),
  profit_protect_values: tuple[float, ...] = (0.5, 0.75, 1.0),
) -> dict[str, Any]:
  """Small grid on pressing thresholds (holdout PnL only for speed)."""
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  if years > 0:
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=int(years * 365.25))
    df = df[df["timestamp"] >= start].reset_index(drop=True)
  _, df_hold = _split_holdout(df, holdout_frac=holdout_frac)

  rows: list[dict[str, Any]] = []
  for cons_edge in conservative_edges:
    for press_edge in pressing_edges:
      for pp in profit_protect_values:
        tuned = copy.deepcopy(cfg)
        hm = tuned.setdefault("hourly", {}).setdefault("bot", {}).setdefault("hour_momentum", {})
        hm["enabled"] = True
        hm["profit_protect_pnl_usd"] = pp
        hm.setdefault("conservative", {})["late_entry_min_ask_edge_cents"] = cons_edge
        hm.setdefault("pressing", {})["late_entry_min_ask_edge_cents"] = press_edge
        r = run_pressing_variant_backtest(df_hold, tuned, variant="v3_pressing")
        rows.append({
          "conservative_late_edge_cents": cons_edge,
          "pressing_late_edge_cents": press_edge,
          "profit_protect_pnl_usd": pp,
          "holdout_pnl_usd": r["total_pnl_usd"],
          "holdout_filled_enters": r["filled_enters"],
          "holdout_expectancy_per_fill_usd": r["expectancy_per_fill_usd"],
          "holdout_max_drawdown_usd": r["max_drawdown_usd"],
        })

  rows.sort(key=lambda x: x["holdout_pnl_usd"], reverse=True)
  return {"grid_rows": rows, "best": rows[0] if rows else None}
