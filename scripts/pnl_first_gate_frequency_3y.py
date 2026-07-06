#!/usr/bin/env python3
"""3y gate-frequency audit for pnl_first vs looser profiles (synthetic OHLC books).

Replays hourly BTC candles with synthetic Kalshi markets and walks the live gate
stack per poll. Use after mechanics changes — pre-5.0.9 runs showed taker_fail
for every edge-qualified poll because cross_spread was disabled under taker_only.

Output: data/logs/pnl_first_gate_frequency_3y.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.backtest.hourly_mechanics_backtest import (
  _blended_mu_for_poll,
  _brti_poll_prices,
  _pick_from_market,
  _synthetic_markets,
)
from src.backtest.mechanics_profiles import apply_mechanics_profile, apply_live_production_mechanics
from src.config import load_config
from src.data.storage import CandleStorage
from src.trading.bot_entry_presets import effective_bot_entry_strategy
from src.trading.bot_live_exit import apply_live_exit_entry_guards, live_exit_config
from src.trading.entry_strategy import passes_ask_edge_gate, passes_tail_entry_gate, rank_hourly_candidates
from src.trading.hourly_regime import HourlyRegimeFilter
from src.trading.live_entry_price import live_entry_pricing_from_cfg, resolve_live_entry_price
from src.trading.live_inventory_guards import apply_live_inventory_guards
from src.trading.live_range_guards import is_range_pick
from src.trading.pnl_first_gates import pnl_first_live_ev_block_reason

DEFAULT_OUT = ROOT / "data" / "logs" / "pnl_first_gate_frequency_3y.json"

SCENARIOS = [
  ("pnl_first_current", "pnl_first", 0.12, 15),
  ("pnl_first_move10_edge15", "pnl_first", 0.10, 15),
  ("pnl_first_move10_edge12", "pnl_first", 0.10, 12),
  ("pnl_first_move08_edge12", "pnl_first", 0.08, 12),
  ("mechanical_fixes_9c", "mechanical_fixes", 0.12, 9),
  ("current_9c", "current", 0.12, 9),
]


@dataclass
class Counters:
  polls: int = 0
  hours: int = 0
  hours_any_candidate: int = 0
  hours_regime_pass_any_poll: int = 0
  hours_full_gate_pass_any_poll: int = 0
  regime_block_polls: int = 0
  no_s1_candidate_polls: int = 0
  edge_fail_polls: int = 0
  tail_fail_polls: int = 0
  taker_fail_polls: int = 0
  ev_fail_polls: int = 0
  full_pass_polls: int = 0

  def add_hour(self, had_cand: bool, regime_ok: bool, full_ok: bool) -> None:
    self.hours += 1
    if had_cand:
      self.hours_any_candidate += 1
    if regime_ok:
      self.hours_regime_pass_any_poll += 1
    if full_ok:
      self.hours_full_gate_pass_any_poll += 1


def audit_profile(
  df: pd.DataFrame,
  cfg: dict,
  profile: str,
  *,
  min_move: float | None = None,
  min_edge_cents: float | None = None,
) -> dict:
  runtime = apply_live_production_mechanics(cfg, kind="hourly", mode="live")
  runtime = apply_mechanics_profile(runtime, profile)  # type: ignore[arg-type]
  if min_move is not None:
    runtime.setdefault("hourly", {}).setdefault("regime", {})["min_expected_move_pct"] = min_move
  regime_filter = HourlyRegimeFilter(runtime)
  estrat = effective_bot_entry_strategy(runtime, kind="hourly", aggressive=False)
  estrat = apply_live_inventory_guards(estrat, runtime, mode="live", kind="hourly")
  estrat = apply_live_exit_entry_guards(estrat, runtime, mode="live", kind="hourly")
  if min_edge_cents is not None:
    estrat = replace(estrat, min_ask_edge_cents=min_edge_cents)
  pricing = live_entry_pricing_from_cfg(runtime, kind="hourly")
  live_exit_config(runtime, kind="hourly")

  c = Counters()
  opens = df["open"].astype(float).values
  highs = df["high"].astype(float).values
  lows = df["low"].astype(float).values
  closes = df["close"].astype(float).values
  ts = pd.to_datetime(df["timestamp"], utc=True)

  for i in range(24, len(df)):
    open_px = float(opens[i])
    high = float(highs[i])
    low = float(lows[i])
    close = float(closes[i])
    mom_idx = max(0, i - 4)
    mom = (open_px - float(opens[mom_idx])) / float(opens[mom_idx]) * 100.0 if opens[mom_idx] else 0.0
    hour_ts = ts.iloc[i].to_pydatetime()
    sigma = max(30.0, (high - low) * 0.6 + open_px * 0.001)
    markets = _synthetic_markets(open_px)
    polls = _brti_poll_prices(open_px, high, low, close)
    hour_had_cand = hour_regime = hour_full = False

    for pi, brti in enumerate(polls):
      c.polls += 1
      hours_left = max(0.08, 1.0 - pi * 0.22)
      terminal_mu = _blended_mu_for_poll(
        mu_mode="momentum",
        open_px=open_px,
        high=high,
        low=low,
        close=close,
        hour_open=open_px,
        momentum_4h_pct=mom,
        poll_idx=pi,
        poll_prices=polls,
        brti=brti,
        hour_ts=hour_ts,
        sigma=sigma,
        cfg=runtime,
      )
      model_base = 0.5 + 0.5 * math.tanh((terminal_mu - open_px) / sigma)
      candidates = []
      for m in markets:
        st = str(m.get("strike_type") or "")
        floor = m.get("floor_strike")
        if st == "greater" and floor is not None:
          dist = (float(floor) - terminal_mu) / sigma
          mp = max(0.03, min(0.97, 0.5 - 0.45 * math.tanh(dist)))
        else:
          mp = model_base
        pick = _pick_from_market(m, brti, sigma=sigma, model_prob=mp)
        if pick["signal"] in ("BUY YES", "BUY NO"):
          candidates.append((abs(float(pick["edge"])), pick, {}))
      ranked = rank_hourly_candidates([(e, p, {}) for e, p, _ in candidates], estrat=estrat)
      s1_ranked = [r for r in ranked if not is_range_pick(r[3])]
      if not s1_ranked:
        c.no_s1_candidate_polls += 1
        continue
      hour_had_cand = True
      pick = s1_ranked[0][3]
      side = "yes" if pick["signal"] == "BUY YES" else "no"
      expected_move = (terminal_mu - brti) / brti * 100.0 if brti else 0.0
      edge = float(pick.get("edge") or 0)
      regime = regime_filter.evaluate(
        expected_move_pct=expected_move,
        hours_to_settle=hours_left,
        sigma_pct=sigma / brti * 100 if brti else 0,
        edge=edge,
      )
      if not regime.allow_trade:
        c.regime_block_polls += 1
        continue
      hour_regime = True
      ok_edge, _ae = passes_ask_edge_gate(pick, side, estrat.min_ask_edge_cents)
      if not ok_edge:
        c.edge_fail_polls += 1
        continue
      resolved = resolve_live_entry_price(pick, side, pricing=pricing, estrat=estrat)
      if resolved.get("price_cents") is None:
        c.taker_fail_polls += 1
        continue
      ok_tail, _, _ = passes_tail_entry_gate(pick, side, int(resolved["price_cents"]), estrat)
      if not ok_tail:
        c.tail_fail_polls += 1
        continue
      ev_block = pnl_first_live_ev_block_reason(pick, side, runtime) if profile == "pnl_first" else None
      if ev_block:
        c.ev_fail_polls += 1
        continue
      c.full_pass_polls += 1
      hour_full = True
    c.add_hour(hour_had_cand, hour_regime, hour_full)

  n_hours = len(df) - 24
  return {
    "profile": profile,
    "min_expected_move_pct": regime_filter.min_expected_move_pct,
    "min_ask_edge_cents": estrat.min_ask_edge_cents,
    "hours_simulated": n_hours,
    "polls_simulated": c.polls,
    "hours_with_any_s1_candidate": c.hours_any_candidate,
    "hours_regime_pass_any_poll": c.hours_regime_pass_any_poll,
    "hours_full_gate_pass_any_poll": c.hours_full_gate_pass_any_poll,
    "pct_hours_regime_pass": round(100 * c.hours_regime_pass_any_poll / n_hours, 2),
    "pct_hours_would_enter": round(100 * c.hours_full_gate_pass_any_poll / n_hours, 2),
    "poll_breakdown": {
      "regime_block": c.regime_block_polls,
      "no_s1_candidate": c.no_s1_candidate_polls,
      "edge_fail": c.edge_fail_polls,
      "taker_fail": c.taker_fail_polls,
      "tail_fail": c.tail_fail_polls,
      "ev_fail": c.ev_fail_polls,
      "full_pass": c.full_pass_polls,
    },
    "entries_per_week_est": round(c.hours_full_gate_pass_any_poll / (n_hours / 24 / 7), 2),
  }


def main() -> int:
  parser = argparse.ArgumentParser(description="3y pnl_first gate-frequency audit")
  parser.add_argument("--years", type=float, default=3.0)
  parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
  args = parser.parse_args()

  cfg = load_config()
  df = CandleStorage(cfg).load("1h").sort_values("timestamp").reset_index(drop=True)
  end = df["timestamp"].max()
  df = df[df["timestamp"] >= end - pd.Timedelta(days=int(args.years * 365.25))].reset_index(drop=True)

  out = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "code_version_note": "post-5.0.9 taker/cross_spread fix, post-5.0.11 adaptive cross bypass",
    "period": f"{df.timestamp.min()} -> {df.timestamp.max()}",
    "bars": len(df),
    "scenarios": {},
  }
  for name, prof, move, edge in SCENARIOS:
    print(f"running {name}...", flush=True)
    out["scenarios"][name] = audit_profile(df, cfg, prof, min_move=move, min_edge_cents=edge)

  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(out, indent=2), encoding="utf-8")
  print(f"wrote {args.output}", flush=True)

  s = out["scenarios"]["pnl_first_current"]
  bd = s["poll_breakdown"]
  print(
    f"pnl_first_current: full_pass={bd['full_pass']} taker_fail={bd['taker_fail']} "
    f"edge_fail={bd['edge_fail']} hours_enter={s['hours_full_gate_pass_any_poll']}",
    flush=True,
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
