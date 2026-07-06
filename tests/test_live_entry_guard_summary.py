"""Tests for live entry guard summary diagnostics."""

from __future__ import annotations

from src.assets import asset_cfg
from src.config import load_config
from src.trading.live_entry_guard_summary import build_live_entry_guard_summary


def test_eth_standard_trial_guard_summary():
  cfg = asset_cfg(load_config(), "eth")
  summary = build_live_entry_guard_summary(cfg, mode="live", kind="hourly", asset="eth")
  assert summary["live_execution_style"] == "standard_trial"
  assert summary["block_tail_entries"] is True
  assert summary["inventory_guards"] is True
  assert summary["range_band_cap_per_hour"] == 8
  assert summary["btc_comparison_hint"]
  assert any("S2 range cap" in n for n in summary["notes"])


def test_btc_pnl_first_guard_summary():
  cfg = asset_cfg(load_config(), "btc")
  summary = build_live_entry_guard_summary(cfg, mode="live", kind="hourly", asset="btc")
  assert summary["mechanics_profile"] == "pnl_first"
  assert summary["live_execution_style"] == "pnl_first"
  assert summary["block_tail_entries"] is True
  assert summary["inventory_guards"] is True
  assert summary["soft_rally_overlay"] is False
  assert summary["btc_comparison_hint"]
  assert any("P&L-first" in n for n in summary["notes"])
  assert any("5 min to settle" in n for n in summary["notes"])


def test_pnl_first_late_hour_floor():
  from src.backtest.mechanics_profiles import apply_live_production_mechanics

  cfg = {
    "hourly": {"bot": {"live_mechanics_profile": "pnl_first"}, "regime": {"min_hours_to_settle": 0.25}},
    "pnl_first": {"min_hours_to_settle_for_entry": 0.083, "max_hours_to_settle_for_entry": 1.35},
  }
  out = apply_live_production_mechanics(cfg, kind="hourly", mode="live")
  assert out["hourly"]["regime"]["min_hours_to_settle"] == 0.083
  assert out["hourly"]["bot"]["min_hours_to_settle_for_entry"] == 0.083
  assert out["hourly"]["bot"]["max_hours_to_settle_for_entry"] == 1.35


def test_paper_mode_returns_minimal():
  cfg = asset_cfg(load_config(), "eth")
  summary = build_live_entry_guard_summary(cfg, mode="paper", kind="hourly", asset="eth")
  assert summary["mode"] == "paper"
  assert "live_execution_style" not in summary
