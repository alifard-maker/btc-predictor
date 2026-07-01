#!/usr/bin/env python3
"""Replay one settled hourly Kalshi event with current bot mechanics (approximate)."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.backtest.fill_simulator import FillSimulator, OrderStyle
from src.backtest.fee_model import FeeModel
from src.backtest.mechanics_profiles import (
  MechanicsProfile,
  apply_mechanics_profile,
  replay_entry_pricing,
)
from src.config import load_config
from src.data.kalshi import KalshiClient
from src.data.storage import CandleStorage
from src.trading.bot_entry_presets import effective_bot_entry_strategy
from src.trading.bot_live_exit import (
  allow_live_cut_loss,
  apply_live_exit_entry_guards,
  effective_live_take_profit_usd,
  live_exit_config,
)
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
from src.trading.entry_strategy import (
  ask_cents_for_side,
  correlation_block_reason,
  entry_budget_usd,
  passes_ask_edge_gate,
  passes_tail_entry_gate,
  rank_hourly_candidates,
)
from src.trading.live_entry_price import resolve_live_entry_price
from src.trading.paper_execution import _side_quotes_cents


def _event_ticker_from_local(
  year: int, month: int, day: int, hour: int, tz_name: str, series: str = "KXBTCD",
) -> str:
  """Kalshi hourly suffix is always Eastern wall-clock hour."""
  local = datetime(year, month, day, hour, 0, tzinfo=ZoneInfo(tz_name))
  et = local.astimezone(ZoneInfo("America/New_York"))
  suffix = et.strftime("%y%b%d%H").upper()
  return f"{series}-{suffix}"


def _contract_yes_won(
  settle_brti: float,
  *,
  strike_type: str,
  floor_strike: float | None,
  cap_strike: float | None,
) -> bool:
  st = (strike_type or "").lower()
  if st == "greater" and floor_strike is not None:
    return settle_brti >= float(floor_strike)
  if st == "less" and cap_strike is not None:
    return settle_brti < float(cap_strike)
  if st == "between" and floor_strike is not None and cap_strike is not None:
    return float(floor_strike) <= settle_brti <= float(cap_strike)
  return False


def _approx_yes_mid(brti: float, *, strike_type: str, floor: float | None, cap: float | None, sigma: float) -> float:
  """Rough implied yes mid from BRTI vs strike (no historical book)."""
  sig = max(20.0, sigma)
  if strike_type == "greater" and floor is not None:
    z = (brti - float(floor)) / sig
    return max(0.02, min(0.98, 0.5 + 0.4 * math.tanh(z * 0.8)))
  if strike_type == "between" and floor is not None and cap is not None:
    mid = (float(floor) + float(cap)) / 2.0
    z = (brti - mid) / sig
    band = max(1.0, float(cap) - float(floor))
    inside = math.exp(-0.5 * ((brti - mid) / (band * 0.35)) ** 2)
    return max(0.02, min(0.98, 0.15 + 0.7 * inside))
  return 0.5


def _pick_from_market(m: dict[str, Any], brti: float, sigma: float, model_prob: float | None) -> dict[str, Any]:
  st = str(m.get("strike_type") or "")
  floor = m.get("floor_strike")
  cap = m.get("cap_strike")
  yes_mid = _approx_yes_mid(brti, strike_type=st, floor=floor, cap=cap, sigma=sigma)
  if model_prob is not None and st == "greater" and floor is not None:
    # blend model distance with moneyness
    yes_mid = max(0.02, min(0.98, 0.6 * yes_mid + 0.4 * model_prob))
  spread = 0.04
  yes_bid = max(0.01, yes_mid - spread / 2)
  yes_ask = min(0.99, yes_mid + spread / 2)
  edge = (model_prob or yes_mid) - yes_mid if model_prob else 0.0
  signal = "BUY YES" if (model_prob or 0) > yes_mid + 0.08 else (
    "BUY NO" if (model_prob or 1) < yes_mid - 0.08 else "HOLD"
  )
  if st == "greater" and floor is not None:
    label = f"≥ ${float(floor):,.0f}"
  elif st == "between" and floor is not None and cap is not None:
    label = f"${float(floor):,.0f} to {float(cap):,.2f}"
  else:
    label = m.get("title") or m.get("ticker")
  return {
    "ticker": m["ticker"],
    "strike_type": st,
    "floor_strike": floor,
    "cap_strike": cap,
    "yes_bid": yes_bid,
    "yes_ask": yes_ask,
    "kalshi_mid": yes_mid,
    "model_prob": model_prob if model_prob is not None else yes_mid,
    "edge": edge,
    "signal": signal,
    "label": label,
  }


@dataclass
class SimLeg:
  id: str
  ticker: str
  side: str
  contracts: int
  entry_cents: int
  cost_usd: float
  opened_at: datetime
  label: str
  entry_source: str = "normal"
  peak_pnl: float = 0.0


@dataclass
class ReplayState:
  legs: list[SimLeg] = field(default_factory=list)
  cash_at_risk: float = 0.0
  realized_pnl: float = 0.0
  log: list[dict[str, Any]] = field(default_factory=list)
  resting_enter_count: int = 0
  _id: int = 0

  def next_id(self) -> str:
    self._id += 1
    return f"leg-{self._id}"


def _mark_leg(leg: SimLeg, pick: dict[str, Any], brti: float) -> tuple[int, float]:
  bid, ask = _side_quotes_cents(pick, leg.side)
  mark = bid if bid is not None else int(round(float(pick.get("kalshi_mid", 0.5)) * 100))
  if leg.side == "no" and bid is None:
    mark = max(1, min(99, 100 - int(round(float(pick.get("kalshi_mid", 0.5)) * 100))))
  pnl = (mark - leg.entry_cents) * leg.contracts / 100.0
  if leg.side == "no":
    pnl = (leg.entry_cents - mark) * leg.contracts / 100.0 if mark else pnl
  return mark, pnl


def compute_trade_stats(log: list[dict[str, Any]]) -> dict[str, Any]:
  filled_enters = [t for t in log if t.get("action") == "enter" and t.get("status") == "filled"]
  resting_enters = [t for t in log if t.get("action") == "enter" and t.get("status") == "resting"]
  exits = [t for t in log if t.get("action") == "exit"]
  pnls = [float(t.get("pnl_usd") or 0) for t in exits]
  wins = sum(1 for p in pnls if p > 0)
  losses = sum(1 for p in pnls if p < 0)
  flats = sum(1 for p in pnls if p == 0)
  closed = wins + losses + flats
  return {
    "filled_enters": len(filled_enters),
    "resting_enters": len(resting_enters),
    "total_enters_attempted": len(filled_enters) + len(resting_enters),
    "exits": len(exits),
    "wins": wins,
    "losses": losses,
    "flats": flats,
    "win_rate": round(wins / closed, 4) if closed else 0.0,
    "total_pnl_usd": round(sum(pnls), 2),
  }


def replay_event(
  *,
  event_ticker: str,
  cfg: dict[str, Any],
  prediction: dict[str, Any] | None,
  passive: bool = True,
  max_spend: float = 15.0,
  mechanics_profile: MechanicsProfile = "current",
) -> dict[str, Any]:
  cfg = apply_mechanics_profile(cfg, mechanics_profile)
  prediction = prediction or {}
  kalshi = KalshiClient(cfg)
  ev = kalshi.get(f"/events/{event_ticker}").get("event") or {}
  mkts_raw = kalshi.get("/markets", params={"event_ticker": event_ticker, "limit": 200}).get("markets") or []
  settle = float(mkts_raw[0].get("expiration_value") or prediction.get("reference_price") or 0)
  ref = float(prediction.get("reference_price") or settle)
  sigma = float(prediction.get("terminal_sigma") or 70.0)
  terminal_mu = float(prediction.get("terminal_mu") or prediction.get("blended_mu") or ref)

  from src.trading.hourly_event_time import hourly_event_settle_utc

  settle_utc = hourly_event_settle_utc(event_ticker)
  if settle_utc is None:
    raise ValueError(f"Bad event ticker {event_ticker}")
  start_utc = settle_utc - timedelta(hours=1)

  storage = CandleStorage(cfg)
  df_1m = storage.load("1m")
  df_1m["ts"] = pd.to_datetime(df_1m["timestamp"], utc=True)
  path = df_1m[(df_1m["ts"] >= start_utc - timedelta(minutes=5)) & (df_1m["ts"] <= settle_utc + timedelta(minutes=1))]

  settings_aggressive = not passive
  estrat = effective_bot_entry_strategy(cfg, kind="hourly", aggressive=settings_aggressive, tuning=None)
  estrat = apply_live_inventory_guards(estrat, cfg, mode="live", kind="hourly")
  estrat = apply_live_exit_entry_guards(estrat, cfg, mode="live", kind="hourly")
  live_exit = live_exit_config(cfg, kind="hourly")
  base_pricing = replay_entry_pricing(
    cfg, profile=mechanics_profile, aggressive=settings_aggressive, kind="hourly",
  )
  import numpy as np
  fills = FillSimulator(app_cfg=cfg, fee_model=FeeModel(cfg=cfg))
  fills._rng = np.random.default_rng(42)

  state = ReplayState()
  picks_cache: dict[str, dict[str, Any]] = {}

  poll_times = pd.date_range(start_utc, settle_utc - timedelta(minutes=2), freq="10min", tz="UTC")

  for ts in poll_times:
    brti_row = path[path["ts"] <= ts]
    if brti_row.empty:
      continue
    brti = float(brti_row.iloc[-1]["close"])
    hours_left = max(0.05, (settle_utc - ts.to_pydatetime()).total_seconds() / 3600.0)

    # model_prob decays toward terminal view
    model_prob_base = 0.5 + 0.5 * math.tanh((terminal_mu - ref) / max(50.0, sigma))

    candidates: list[tuple[float, dict, dict]] = []
    for m in mkts_raw:
      ticker = str(m.get("ticker") or "")
      st = str(m.get("strike_type") or "")
      floor = m.get("floor_strike")
      if st == "greater" and floor is not None:
        dist = (float(floor) - terminal_mu) / sigma
        mp = max(0.03, min(0.97, 0.5 - 0.45 * math.tanh(dist)))
      elif st == "between" and floor is not None:
        mp = 0.25
      else:
        mp = model_prob_base
      pick = _pick_from_market(m, brti, sigma=sigma, model_prob=mp)
      picks_cache[ticker] = pick
      if pick["signal"] in ("BUY YES", "BUY NO"):
        edge = abs(float(pick.get("model_prob", 0.5)) - float(pick.get("kalshi_mid", 0.5)))
        candidates.append((edge, pick, {}))

    ranked = rank_hourly_candidates(
      [(e, p, {}) for e, p, _ in candidates], estrat=estrat,
    )

    # exits first
    for leg in list(state.legs):
      pick = picks_cache.get(leg.ticker) or {}
      mark, unreal = _mark_leg(leg, pick, brti)
      leg.peak_pnl = max(leg.peak_pnl, unreal)
      hold_s = (ts.to_pydatetime() - leg.opened_at).total_seconds()
      pos = {
        "opened_at": leg.opened_at.isoformat(),
        "entry_price_cents": leg.entry_cents,
        "entry_source": leg.entry_source,
        "market_ticker": leg.ticker,
        "side": leg.side,
      }
      tp_usd = effective_live_take_profit_usd(
        pos, live_exit.take_profit_usd or 0.08, cfg, kind="hourly",
      )
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
        state.log.append({
          "at": ts.isoformat(), "action": "exit", "reason": exit_reason,
          "ticker": leg.ticker, "label": leg.label, "side": leg.side,
          "contracts": leg.contracts, "entry_cents": leg.entry_cents,
          "exit_cents": mark, "pnl_usd": round(pnl, 2), "brti": round(brti, 2),
        })
        state.legs.remove(leg)

    # entries
    tab = {
      "live": {
        "current_price": brti,
        "expected_move_pct": (terminal_mu - brti) / brti * 100.0 if brti else 0.0,
        "terminal_mu": terminal_mu,
        "blended_mu": terminal_mu,
        "regime": {
          "allow_trade": abs(terminal_mu - brti) >= sigma * 0.001,
          "reasons": [],
        },
        "primary_pick": ranked[0][3] if ranked else None,
        "strategy_threshold": {"best_edge": ranked[0][3] if ranked else None, "contracts": []},
        "strategy_range": {"best_edge": None, "contracts": []},
      },
      "locked": {"reference_price": ref},
      "hour_open": {"reference_price": ref},
      "brti_live": brti,
    }
    adaptive = assess_adaptive_passive_mode(
      tab=tab,
      cfg=cfg,
      realized_pnl_usd=state.realized_pnl,
      aggressive=settings_aggressive,
      mode="live",
    )
    if adaptive.mode == "locked":
      continue
    if defense_entries_blocked(adaptive, cfg):
      continue

    estrat_poll = apply_adaptive_passive_guards(estrat, adaptive, cfg)
    pricing = adaptive_live_entry_pricing(base_pricing, adaptive, cfg)

    entries = 0
    for _c, _e, _s, pick, _bet in ranked[:estrat_poll.max_entries_per_cycle]:
      if entries >= estrat_poll.max_entries_per_cycle:
        break
      if len(state.legs) >= estrat_poll.max_concurrent_positions:
        break
      if state.cash_at_risk >= max_spend:
        break
      side = "yes" if pick["signal"] == "BUY YES" else "no"
      range_block = adaptive_range_band_block_reason(pick, adaptive, cfg)
      if range_block:
        continue
      defense_block = adaptive_defense_entry_block_reason(pick, side, adaptive, cfg)
      if defense_block:
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
        and mechanics_profile in ("current", "rally_only", "soft_rally")
      ):
        from dataclasses import replace as dc_replace

        resolved = resolve_live_entry_price(
          pick, side,
          pricing=dc_replace(pricing, cross_spread_enabled=False),
          estrat=estrat_poll,
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
        pick=pick, side=side, entries_left=1,
      )
      count = max(1, int(stake // (price_cents / 100.0))) if price_cents else 0
      adopted_cap = live_exit.max_adopted_contracts or 2
      if mechanics_profile == "legacy":
        count = min(count, 6)
      else:
        count = min(count, adopted_cap)
      if count <= 0:
        continue
      prob_up = float(pick.get("kalshi_mid") or 0.5)
      order_style = (
        OrderStyle.CROSS_SPREAD
        if resolved.get("execution_mode") == "cross_spread"
        else OrderStyle.PASSIVE_LIMIT
      )
      fill_res = fills.simulate_entry(
        prob_up=prob_up,
        side=side,
        order_style=order_style,
        time_to_settle_hours=hours_left,
        spread_cents=int(pick.get("yes_ask", 0.5) * 100 - pick.get("yes_bid", 0.5) * 100) if pick.get("yes_ask") else 4,
      )
      state.resting_enter_count += 1
      if not fill_res.filled or fill_res.price_cents is None:
        state.log.append({
          "at": ts.isoformat(), "action": "enter", "status": "resting",
          "ticker": pick["ticker"], "label": pick["label"], "side": side,
          "price_cents": price_cents, "brti": round(brti, 2),
          "fill_prob": round(fill_res.fill_probability, 2),
          "entry_mode": adaptive.mode,
        })
        continue
      fill_px = fill_res.price_cents
      cost = round(count * fill_px / 100.0, 2)
      leg = SimLeg(
        id=state.next_id(),
        ticker=pick["ticker"],
        side=side,
        contracts=count,
        entry_cents=fill_px,
        cost_usd=cost,
        opened_at=ts.to_pydatetime(),
        label=str(pick.get("label") or ""),
      )
      state.legs.append(leg)
      state.cash_at_risk += cost
      entries += 1
      state.log.append({
        "at": ts.isoformat(), "action": "enter", "status": "filled",
        "mode": resolved["execution_mode"], "ticker": pick["ticker"],
        "label": pick["label"], "side": side, "contracts": count,
        "price_cents": fill_px, "cost_usd": cost, "brti": round(brti, 2),
        "entry_mode": adaptive.mode,
      })

  # settlement on remaining legs
  for leg in list(state.legs):
    m = next((x for x in mkts_raw if x.get("ticker") == leg.ticker), {})
    won = _contract_yes_won(
      settle,
      strike_type=str(m.get("strike_type") or ""),
      floor_strike=m.get("floor_strike"),
      cap_strike=m.get("cap_strike"),
    )
    win = won if leg.side == "yes" else not won
    exit_c = 100 if win else 0
    if leg.side == "yes":
      pnl = (exit_c - leg.entry_cents) * leg.contracts / 100.0
    else:
      pnl = (leg.entry_cents - exit_c) * leg.contracts / 100.0
    state.realized_pnl += pnl
    state.log.append({
      "at": settle_utc.isoformat(), "action": "exit", "reason": "SETTLEMENT",
      "ticker": leg.ticker, "label": leg.label, "won": win,
      "exit_cents": exit_c, "pnl_usd": round(pnl, 2), "settle_brti": settle,
    })
    state.legs.clear()

  brti_path = {
    "open": float(path.iloc[0]["close"]) if len(path) else None,
    "low": float(path["low"].min()) if len(path) else None,
    "high": float(path["high"].max()) if len(path) else None,
    "pre_settle": float(path[path["ts"] <= settle_utc].iloc[-1]["close"]) if len(path) else None,
    "settle": settle,
  }

  stats = compute_trade_stats(state.log)
  return {
    "event_ticker": event_ticker,
    "title": ev.get("title"),
    "settle_utc": settle_utc.isoformat(),
    "passive": passive,
    "mechanics_profile": mechanics_profile,
    "max_spend_usd": max_spend,
    "prediction_snapshot": prediction,
    "brti_path": brti_path,
    "realized_pnl_usd": round(state.realized_pnl, 2),
    "trade_stats": stats,
    "trades": state.log,
    "summary_by_reason": _summarize(state.log),
    "disclaimer": "Approximate replay: synthetic intrahour contract mids from BRTI; not historical Kalshi books.",
  }


def _summarize(log: list[dict[str, Any]]) -> dict[str, Any]:
  out: dict[str, Any] = {}
  for row in log:
    if row.get("action") != "exit":
      continue
    k = str(row.get("reason") or "OTHER")
    out.setdefault(k, {"n": 0, "pnl": 0.0})
    out[k]["n"] += 1
    out[k]["pnl"] += float(row.get("pnl_usd") or 0)
  for k in out:
    out[k]["pnl"] = round(out[k]["pnl"], 2)
  return out


def main() -> None:
  p = argparse.ArgumentParser(description="Replay one hourly event with current bot mechanics")
  p.add_argument("--event", default=None, help="e.g. KXBTCD-26JUN2802")
  p.add_argument("--date", default="2026-06-28")
  p.add_argument("--hour", type=int, default=2)
  p.add_argument("--tz", default="America/New_York", help="Local tz for --hour (Kalshi suffix uses Eastern)")
  p.add_argument("--aggressive", action="store_true")
  p.add_argument("--output", default=None)
  args = p.parse_args()

  cfg = load_config()
  if args.event:
    event = args.event.upper()
  else:
    y, m, d = map(int, args.date.split("-"))
    event = _event_ticker_from_local(y, m, d, args.hour, args.tz)

  # production prediction if available
  prediction = None
  try:
    import requests
    from pathlib import Path as P
    env = P(__file__).resolve().parents[1] / ".env"
    pw = None
    if env.exists():
      for line in env.read_text().splitlines():
        if line.startswith("APP_PASSWORD="):
          pw = line.split("=", 1)[1].strip()
    if pw:
      s = requests.Session()
      s.post(
        f"{cfg.get('production_url', 'https://btc-predictor-production-f460.up.railway.app')}/api/auth/login",
        data={"password": pw}, timeout=15,
      )
      rows = s.get(
        f"{cfg.get('production_url', 'https://btc-predictor-production-f460.up.railway.app')}/api/hourly/predictions",
        params={"limit": 200}, timeout=15,
      ).json()
      if isinstance(rows, dict):
        rows = rows.get("predictions") or []
      prediction = next((r for r in rows if r.get("event_ticker") == event), None)
  except Exception:
    pass

  result = replay_event(
    event_ticker=event,
    cfg=cfg,
    prediction=prediction or {},
    passive=not args.aggressive,
    max_spend=float((cfg.get("hourly") or {}).get("bot", {}).get("max_spend_per_hour_usd", 15) or 15),
  )

  out_path = Path(args.output) if args.output else Path("data/logs") / f"replay_{event}.json"
  out_path.parent.mkdir(parents=True, exist_ok=True)
  out_path.write_text(json.dumps(result, indent=2, default=str))
  print(json.dumps({
    "event": result["event_ticker"],
    "settle_brti": result["brti_path"]["settle"],
    "brti_path": result["brti_path"],
    "pnl": result["realized_pnl_usd"],
    "summary": result["summary_by_reason"],
    "n_trades": len(result["trades"]),
    "output": str(out_path),
  }, indent=2))


if __name__ == "__main__":
  main()
