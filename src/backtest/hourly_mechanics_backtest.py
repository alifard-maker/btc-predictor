"""3-year hourly mechanics backtest from 1h OHLC (synthetic Kalshi books)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.features.path_memory import apply_path_memory_adjustment, path_memory_from_1m

from src.backtest.fill_simulator import FillSimulator, OrderStyle
from src.backtest.fee_model import FeeModel
from src.backtest.mechanics_profiles import (
  MechanicsProfile,
  PROFILE_LABELS,
  apply_mechanics_profile,
  replay_entry_pricing,
)
from src.trading.bot_entry_presets import effective_bot_entry_strategy
from src.trading.bot_live_exit import (
  allow_live_cut_loss,
  apply_live_exit_entry_guards,
  effective_live_take_profit_usd,
  live_exit_config,
)
from src.trading.entry_strategy import (
  CycleEntryBudget,
  ask_edge_cents_for_pick,
  correlation_block_reason,
  entry_budget_usd,
  passes_ask_edge_gate,
  passes_tail_entry_gate,
  rank_hourly_candidates,
  side_from_pick_signal,
)
from src.trading.hourly_regime import HourlyRegimeFilter
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


@dataclass
class SimLeg:
  ticker: str
  side: str
  contracts: int
  entry_cents: int
  cost_usd: float
  opened_at: datetime
  label: str


@dataclass
class MechanicsSimOptions:
  """Optional overrides for focused profile comparisons (backtest only)."""

  use_live_regime: bool = False
  rank_by_ask_edge: bool = False
  cut_loss_min_usd: float | None = None
  disable_cut_loss: bool = False
  structure_memory: bool = False
  structure_lookback_bars: int = 12


@dataclass
class HourSimState:
  legs: list[SimLeg] = field(default_factory=list)
  cash_at_risk: float = 0.0
  realized_pnl: float = 0.0
  resting_enter_count: int = 0
  filled_enters: int = 0
  resting_enters: int = 0
  exits: int = 0
  wins: int = 0
  losses: int = 0
  by_reason: dict[str, dict[str, float]] = field(default_factory=dict)


def _summarize_reason(state: HourSimState, reason: str, pnl: float) -> None:
  bucket = state.by_reason.setdefault(reason, {"n": 0, "pnl": 0.0})
  bucket["n"] += 1
  bucket["pnl"] += pnl


def _synthetic_markets(open_px: float) -> list[dict[str, Any]]:
  base = int(round(open_px / 100.0)) * 100
  out: list[dict[str, Any]] = []
  for floor in (base - 200, base - 100, base, base + 100, base + 200):
    out.append({
      "ticker": f"SYN-T{floor}",
      "strike_type": "greater",
      "floor_strike": float(floor),
      "cap_strike": None,
    })
  for lo in (base - 200, base - 100, base, base + 100):
    out.append({
      "ticker": f"SYN-R{lo}",
      "strike_type": "between",
      "floor_strike": float(lo),
      "cap_strike": float(lo + 99.99),
    })
  return out


def _approx_yes_mid(brti: float, *, strike_type: str, floor: float | None, cap: float | None, sigma: float) -> float:
  sig = max(20.0, sigma)
  if strike_type == "greater" and floor is not None:
    z = (brti - float(floor)) / sig
    return max(0.02, min(0.98, 0.5 + 0.4 * math.tanh(z * 0.8)))
  if strike_type == "between" and floor is not None and cap is not None:
    mid = (float(floor) + float(cap)) / 2.0
    band = max(1.0, float(cap) - float(floor))
    inside = math.exp(-0.5 * ((brti - mid) / (band * 0.35)) ** 2)
    return max(0.02, min(0.98, 0.15 + 0.7 * inside))
  return 0.5


def _pick_from_market(m: dict[str, Any], brti: float, *, sigma: float, model_prob: float) -> dict[str, Any]:
  st = str(m.get("strike_type") or "")
  floor = m.get("floor_strike")
  cap = m.get("cap_strike")
  yes_mid = _approx_yes_mid(brti, strike_type=st, floor=floor, cap=cap, sigma=sigma)
  if st == "greater" and floor is not None:
    yes_mid = max(0.02, min(0.98, 0.6 * yes_mid + 0.4 * model_prob))
  spread = 0.04
  yes_bid = max(0.01, yes_mid - spread / 2)
  yes_ask = min(0.99, yes_mid + spread / 2)
  edge = model_prob - yes_mid
  signal = "BUY YES" if edge > 0.08 else ("BUY NO" if edge < -0.08 else "HOLD")
  if st == "greater" and floor is not None:
    label = f"≥ ${float(floor):,.0f}"
  elif st == "between" and floor is not None and cap is not None:
    label = f"${float(floor):,.0f} band"
  else:
    label = m["ticker"]
  return {
    "ticker": m["ticker"],
    "strike_type": st,
    "floor_strike": floor,
    "cap_strike": cap,
    "yes_bid": yes_bid,
    "yes_ask": yes_ask,
    "kalshi_mid": yes_mid,
    "model_prob": model_prob,
    "edge": edge,
    "signal": signal,
    "label": label,
  }


def _contract_yes_won(settle: float, m: dict[str, Any]) -> bool:
  st = str(m.get("strike_type") or "").lower()
  floor = m.get("floor_strike")
  cap = m.get("cap_strike")
  if st == "greater" and floor is not None:
    return settle >= float(floor)
  if st == "between" and floor is not None and cap is not None:
    return float(floor) <= settle <= float(cap)
  return False


def _mark_leg(leg: SimLeg, pick: dict[str, Any]) -> tuple[int, float]:
  bid = int(round(float(pick.get("yes_bid", pick.get("kalshi_mid", 0.5))) * 100))
  if leg.side == "yes":
    mark = bid
    pnl = (mark - leg.entry_cents) * leg.contracts / 100.0
  else:
    mark = max(1, min(99, 100 - bid))
    pnl = (leg.entry_cents - mark) * leg.contracts / 100.0
  return mark, pnl


def _brti_poll_prices(open_px: float, high: float, low: float, close: float) -> list[float]:
  return [open_px, low + 0.35 * (high - low), low + 0.65 * (high - low), close]


MuMode = Literal["momentum", "v2_path"]


def _synthetic_1m_to_poll(
  hour_ts: datetime,
  poll_prices: list[float],
  poll_idx: int,
) -> pd.DataFrame:
  """Build minimal 1m path from hour open through the current poll."""
  rows: list[dict[str, Any]] = []
  base = pd.Timestamp(hour_ts)
  if base.tzinfo is None:
    base = base.tz_localize(timezone.utc)
  else:
    base = base.tz_convert(timezone.utc)
  for j in range(poll_idx + 1):
    offset_min = j * 15 if j else 0
    rows.append({"timestamp": base + pd.Timedelta(minutes=offset_min), "close": poll_prices[j]})
  return pd.DataFrame(rows)


def _blended_mu_for_poll(
  *,
  mu_mode: MuMode,
  open_px: float,
  high: float,
  low: float,
  close: float,
  hour_open: float,
  momentum_4h_pct: float,
  poll_idx: int,
  poll_prices: list[float],
  brti: float,
  hour_ts: datetime,
  sigma: float,
  cfg: dict[str, Any],
) -> float:
  structure_mu = open_px * (1.0 + momentum_4h_pct / 100.0 * 0.25)
  if mu_mode == "momentum":
    return structure_mu

  hcfg = cfg.get("hourly_v2", {})
  v2cal = hcfg.get("_v2_calibration") or {}
  sigma_scale = float(v2cal.get("sigma_scale", 1.0))
  effective_sigma = sigma * sigma_scale
  path_weight = float(hcfg.get("blend", {}).get("path_weight", 0.55))
  structure_weight = float(hcfg.get("blend", {}).get("structure_weight", 0.45))
  hours_left = max(0.08, 1.0 - poll_idx * 0.22)
  hour_start = pd.Timestamp(hour_ts)
  if hour_start.tzinfo is None:
    hour_start = hour_start.tz_localize(timezone.utc)
  else:
    hour_start = hour_start.tz_convert(timezone.utc)
  poll_ts = hour_start + pd.Timedelta(minutes=poll_idx * 15)
  df_1m = _synthetic_1m_to_poll(hour_ts, poll_prices, poll_idx)
  path = path_memory_from_1m(
    df_1m,
    hour_open=hour_start,
    lock_price=hour_open,
    current_price=brti,
    as_of=poll_ts,
  )
  path_mu, _ = apply_path_memory_adjustment(
    structure_mu,
    effective_sigma,
    path,
    hours_left,
    cfg=hcfg,
  )
  return path_weight * path_mu + structure_weight * structure_mu


def simulate_hour(
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
  sim_options: MechanicsSimOptions | None = None,
  df_history: pd.DataFrame | None = None,
) -> HourSimState:
  sim_options = sim_options or MechanicsSimOptions()
  cfg = apply_mechanics_profile(cfg, profile)
  state = HourSimState()
  sigma = max(30.0, (high - low) * 0.6 + open_px * 0.001)
  markets = _synthetic_markets(open_px)
  settle = close

  estrat = effective_bot_entry_strategy(cfg, kind="hourly", aggressive=False, tuning=None)
  estrat = apply_live_inventory_guards(estrat, cfg, mode="live", kind="hourly")
  estrat = apply_live_exit_entry_guards(estrat, cfg, mode="live", kind="hourly")
  live_exit = live_exit_config(cfg, kind="hourly")
  if sim_options.cut_loss_min_usd is not None:
    from dataclasses import replace as dc_replace

    live_exit = dc_replace(live_exit, cut_loss_min_usd=float(sim_options.cut_loss_min_usd))
  regime_filter = HourlyRegimeFilter(cfg) if sim_options.use_live_regime else None
  base_pricing = replay_entry_pricing(cfg, profile=profile, aggressive=False, kind="hourly")

  polls = _brti_poll_prices(open_px, high, low, close)
  picks_cache: dict[str, dict[str, Any]] = {}
  structure_detail: dict[str, Any] = {}

  for i, brti in enumerate(polls):
    hours_left = max(0.08, 1.0 - i * 0.22)
    terminal_mu = _blended_mu_for_poll(
      mu_mode=mu_mode,
      open_px=open_px,
      high=high,
      low=low,
      close=close,
      hour_open=hour_open,
      momentum_4h_pct=momentum_4h_pct,
      poll_idx=i,
      poll_prices=polls,
      brti=brti,
      hour_ts=hour_ts,
      sigma=sigma,
      cfg=cfg,
    )
    poll_sigma = sigma
    if sim_options.structure_memory and df_history is not None:
      from src.backtest.structure_memory import (
        StructureMemoryConfig,
        adjust_mu_sigma_from_structure,
        structure_blocks_yes_above,
      )

      terminal_mu, poll_sigma, structure_detail = adjust_mu_sigma_from_structure(
        terminal_mu,
        poll_sigma,
        brti,
        df_history,
        cfg=StructureMemoryConfig(lookback_bars=sim_options.structure_lookback_bars),
      )
    model_base = 0.5 + 0.5 * math.tanh((terminal_mu - hour_open) / poll_sigma)
    candidates: list[tuple[float, dict, dict]] = []
    for m in markets:
      st = str(m.get("strike_type") or "")
      floor = m.get("floor_strike")
      if st == "greater" and floor is not None:
        dist = (float(floor) - terminal_mu) / poll_sigma
        mp = max(0.03, min(0.97, 0.5 - 0.45 * math.tanh(dist)))
      else:
        mp = model_base
      pick = _pick_from_market(m, brti, sigma=poll_sigma, model_prob=mp)
      picks_cache[pick["ticker"]] = pick
      if pick["signal"] in ("BUY YES", "BUY NO"):
        if sim_options.rank_by_ask_edge:
          side = side_from_pick_signal(pick.get("signal")) or "yes"
          ask_edge = ask_edge_cents_for_pick(pick, side) or 0.0
          candidates.append((ask_edge / 100.0, pick, {}))
        else:
          candidates.append((abs(float(pick["edge"])), pick, {}))

    ranked = rank_hourly_candidates([(e, p, {}) for e, p, _ in candidates], estrat=estrat)
    if sim_options.rank_by_ask_edge:

      def _ask_rank(row: tuple) -> float:
        side = side_from_pick_signal(row[3].get("signal")) or "yes"
        return ask_edge_cents_for_pick(row[3], side) or 0.0

      ranked = sorted(ranked, key=lambda row: (-_ask_rank(row), -row[2], -row[1]))

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
      elif (
        not sim_options.disable_cut_loss
        and unreal <= -live_exit.cut_loss_min_usd
        and allow_live_cut_loss(
          exit_reason="CUT LOSSES",
          unrealized_usd=unreal,
          pos=pos,
          settings_min_hold=120,
          cfg=cfg,
          kind="hourly",
        )
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
    sigma_pct = (sigma / brti * 100.0) if brti else 0.0
    primary_edge = abs(float(ranked[0][3].get("edge") or 0)) if ranked else None
    if regime_filter is not None:
      regime_decision = regime_filter.evaluate(
        expected_move_pct=expected_move,
        hours_to_settle=hours_left,
        sigma_pct=sigma_pct,
        edge=primary_edge,
      )
      regime_allow = regime_decision.allow_trade
      regime_reasons = regime_decision.reasons
    else:
      regime_allow = abs(momentum_4h_pct) >= 0.12 or abs(expected_move) >= 0.12
      regime_reasons = []
    tab = {
      "live": {
        "current_price": brti,
        "expected_move_pct": expected_move,
        "terminal_mu": terminal_mu,
        "blended_mu": terminal_mu,
        "regime": {"allow_trade": regime_allow, "reasons": regime_reasons},
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
    if sim_options.use_live_regime and not regime_allow:
      continue

    estrat_poll = apply_adaptive_passive_guards(estrat, adaptive, cfg)
    pricing = adaptive_live_entry_pricing(base_pricing, adaptive, cfg)
    cycle_budget = CycleEntryBudget(estrat_poll)
    for _c, _e, _s, pick, _bet in ranked[:cycle_budget.max_cycle_candidates()]:
      if not cycle_budget.can_enter(pick):
        continue
      if len(state.legs) >= estrat_poll.max_concurrent_positions:
        break
      if state.cash_at_risk >= max_spend:
        break
      side = "yes" if pick["signal"] == "BUY YES" else "no"
      if (
        sim_options.structure_memory
        and side == "yes"
        and str(pick.get("strike_type") or "") == "greater"
        and structure_blocks_yes_above(structure_detail)
      ):
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
      ok, _ = passes_ask_edge_gate(pick, side, estrat_poll.min_ask_edge_cents)
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


def pick_edge_ok(ranked: list) -> bool:
  if not ranked:
    return False
  return abs(float(ranked[0][3].get("edge") or 0)) >= 0.08


def run_mechanics_backtest(
  df_1h: pd.DataFrame,
  cfg: dict[str, Any],
  *,
  profile: MechanicsProfile,
  max_spend: float = 15.0,
  warmup_bars: int = 24,
  mu_mode: MuMode = "momentum",
  sim_options: MechanicsSimOptions | None = None,
) -> dict[str, Any]:
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  fills = FillSimulator(app_cfg=cfg, fee_model=FeeModel(cfg=cfg))
  fills._rng = np.random.default_rng(42)

  total_pnl = 0.0
  filled = resting = exits = wins = losses = 0
  win_hours = lose_hours = flat_hours = 0
  by_reason: dict[str, dict[str, float]] = {}
  hours_traded = 0

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
    st = simulate_hour(
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
      sim_options=sim_options,
      df_history=df.iloc[: i + 1],
    )
    total_pnl += st.realized_pnl
    filled += st.filled_enters
    resting += st.resting_enters
    exits += st.exits
    wins += st.wins
    losses += st.losses
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

  n_hours = len(df) - warmup_bars
  closed = wins + losses
  for k in by_reason:
    by_reason[k]["pnl"] = round(by_reason[k]["pnl"], 2)

  return {
    "profile": profile,
    "mu_mode": mu_mode,
    "label": PROFILE_LABELS.get(profile, profile),
    "hours_simulated": n_hours,
    "hours_with_fills": hours_traded,
    "period_start": str(ts.iloc[warmup_bars]),
    "period_end": str(ts.iloc[-1]),
    "total_pnl_usd": round(total_pnl, 2),
    "avg_pnl_per_hour_usd": round(total_pnl / n_hours, 4),
    "filled_enters": filled,
    "resting_enters": resting,
    "exits": exits,
    "wins": wins,
    "losses": losses,
    "win_rate": round(wins / closed, 4) if closed else 0.0,
    "winning_hours": win_hours,
    "losing_hours": lose_hours,
    "flat_hours": flat_hours,
    "by_exit_type": by_reason,
    "expectancy_per_fill_usd": round(total_pnl / filled, 4) if filled else 0.0,
  }
