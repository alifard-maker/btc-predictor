"""Tests for V3 pressing-mode mechanics backtest."""

from __future__ import annotations

import pandas as pd

from src.backtest.hourly_v3_pressing import (
  _hours_left_for_poll,
  _poll_prices_with_late_window,
  run_pressing_variant_backtest,
  run_v3_pressing_comparison,
  simulate_hour_pressing,
)
from src.backtest.fill_simulator import FillSimulator
from src.backtest.fee_model import FeeModel


def _mini_df(n: int = 40) -> pd.DataFrame:
  rows = []
  px = 90000.0
  for i in range(n):
    o = px + i * 10
    rows.append({
      "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=i),
      "open": o,
      "high": o + 200,
      "low": o - 150,
      "close": o + 50,
    })
  return pd.DataFrame(rows)


def _cfg(**hm_overrides):
  base = {
    "hourly": {
      "bot": {
        "live_adaptive": {"enabled": True, "profit_lock_usd": 1.25},
        "live_inventory": {"enabled": True},
        "live_exit": {"max_resting_enters_per_hour": 6, "max_adopted_contracts": 2},
        "live_entry": {"cross_spread_enabled": True},
        "late_entry": {"enabled": True, "min_hours": 0.08, "min_ask_edge_cents": 15},
        "hour_momentum": {
          "enabled": True,
          "profit_protect_pnl_usd": 0.75,
          "min_closed_wins_to_press": 1,
          **hm_overrides,
        },
      },
      "regime": {"enabled": True},
    },
    "fees": {"kalshi_taker_pct": 0.07},
  }
  return base


def test_late_poll_in_window():
  assert _hours_left_for_poll(4) < 0.25
  assert _hours_left_for_poll(4) >= 0.08
  prices = _poll_prices_with_late_window(90000, 90200, 89800, 90100)
  assert len(prices) == 5


def test_simulate_hour_pressing_runs_all_variants():
  cfg = _cfg()
  fills = FillSimulator(app_cfg=cfg, fee_model=FeeModel(cfg=cfg))
  for variant in ("baseline_a", "baseline_b_static", "v3_pressing"):
    st = simulate_hour_pressing(
      open_px=90000.0,
      high=90200.0,
      low=89800.0,
      close=90100.0,
      hour_open=90000.0,
      momentum_4h_pct=0.5,
      cfg=cfg,
      profile="current",
      max_spend=15.0,
      fills=fills,
      hour_ts=pd.Timestamp("2024-06-01", tz="UTC").to_pydatetime(),
      variant=variant,  # type: ignore[arg-type]
    )
    assert st.exits >= 0


def test_v3_records_momentum_states():
  cfg = _cfg()
  r = run_pressing_variant_backtest(_mini_df(), cfg, variant="v3_pressing", warmup_bars=5)
  assert "momentum_state_polls" in r
  assert r["hours_simulated"] == 35


def test_comparison_includes_holdout():
  cfg = _cfg()
  out = run_v3_pressing_comparison(cfg, _mini_df(80), years=0, holdout_frac=0.30, warmup_bars=5)
  assert "full_period" in out
  assert "holdout" in out
  assert "baseline_a" in out["holdout"]
  assert out["holdout_bars"] > 0
