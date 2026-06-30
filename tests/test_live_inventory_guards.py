"""Tests for live-mode inventory guard overlays."""

from __future__ import annotations

from src.trading.bot_entry_presets import effective_bot_entry_strategy
from src.trading.live_inventory_guards import apply_live_inventory_guards


def test_apply_live_inventory_guards_forces_correlation_on_aggressive():
  estrat = effective_bot_entry_strategy({}, kind="hourly", aggressive=True, tuning=None)
  assert estrat.correlation_guard is True
  assert estrat.max_concurrent_positions == 6
  assert estrat.max_entries_per_cycle == 5
  assert estrat.allow_scale_in is True

  guarded = apply_live_inventory_guards(estrat, {}, mode="live", kind="hourly")
  assert guarded.correlation_guard is True
  assert guarded.correlation_min_strike_gap_pct == 0.18
  assert guarded.max_same_side_threshold_legs == 1
  assert guarded.max_same_side_range_legs == 1
  assert guarded.max_concurrent_positions == 4
  assert guarded.max_entries_per_cycle == 2
  assert guarded.allow_scale_in is False


def test_apply_live_inventory_guards_noop_in_paper_mode():
  estrat = effective_bot_entry_strategy({}, kind="hourly", aggressive=True, tuning=None)
  guarded = apply_live_inventory_guards(estrat, {}, mode="paper", kind="hourly")
  assert guarded is estrat


def test_apply_live_inventory_guards_respects_config_override():
  cfg = {
    "hourly": {
      "bot": {
        "live_inventory": {
          "enabled": True,
          "max_concurrent_positions": 3,
          "allow_scale_in": True,
        }
      }
    }
  }
  estrat = effective_bot_entry_strategy(cfg, kind="hourly", aggressive=False, tuning=None)
  guarded = apply_live_inventory_guards(estrat, cfg, mode="live", kind="hourly")
  assert guarded.max_concurrent_positions == 3
  assert guarded.allow_scale_in is True


def test_apply_live_inventory_guards_disabled_via_config():
  cfg = {"hourly": {"bot": {"live_inventory": {"enabled": False}}}}
  estrat = effective_bot_entry_strategy(cfg, kind="hourly", aggressive=True, tuning=None)
  guarded = apply_live_inventory_guards(estrat, cfg, mode="live", kind="hourly")
  assert guarded is estrat
