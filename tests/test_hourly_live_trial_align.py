"""Tests for live hourly ↔ paper trial alignment."""

from __future__ import annotations

from src.trading.bot_live_exit import quick_exit_config
from src.trading.bot_whipsaw_guard import WhipsawGuardConfig
from src.trading.hourly_live_trial_align import (
  HourlyLiveTrialAlignConfig,
  apply_align_entry_pricing,
  merge_whipsaw_align_overrides,
  should_use_trial_leg_exits,
  skip_soft_rally_entry_overlay,
)
from src.trading.live_entry_price import LiveEntryPricingConfig


def _cfg() -> dict:
  return {
    "hourly": {
      "bot": {
        "live_trial_align": {
          "enabled": True,
          "live_exit_mode": "hybrid",
          "hybrid": {
            "adaptive_modes": ["defense"],
            "hour_momentum_states": ["conservative"],
            "max_hold_seconds": 600,
          },
          "quick_exit": {"min_hold_seconds": 60, "cut_loss_min_hold_seconds": 60},
          "whipsaw": {"max_quick_exit_cuts_per_hour": 2},
        },
        "quick_exit": {"enabled": True, "min_hold_seconds": 30},
      }
    }
  }


def test_should_use_trial_legs_hybrid_defense():
  assert should_use_trial_leg_exits(
    _cfg(), kind="hourly", mode="live", hold_seconds=900,
    adaptive_mode="defense", hour_momentum_state="normal",
  )
  assert not should_use_trial_leg_exits(
    _cfg(), kind="hourly", mode="live", hold_seconds=900,
    adaptive_mode="rally", hour_momentum_state="normal",
  )
  assert should_use_trial_leg_exits(
    _cfg(), kind="hourly", mode="live", hold_seconds=120,
    adaptive_mode="rally", hour_momentum_state="normal",
  )


def test_trial_exit_mode_full():
  cfg = _cfg()
  cfg["hourly"]["bot"]["live_trial_align"]["live_exit_mode"] = "trial_legs"
  assert should_use_trial_leg_exits(
    cfg, kind="hourly", mode="live", hold_seconds=5000,
    adaptive_mode="rally", hour_momentum_state="normal",
  )


def test_quick_exit_align_override():
  q = quick_exit_config(_cfg(), kind="hourly")
  assert q.min_hold_seconds == 60
  assert q.cut_loss_min_hold_seconds == 60


def test_whipsaw_align_override():
  w = merge_whipsaw_align_overrides(WhipsawGuardConfig.from_cfg(_cfg(), kind="hourly"), _cfg())
  assert w.max_quick_exit_cuts_per_hour == 2


def test_skip_soft_rally_when_align_enabled():
  assert skip_soft_rally_entry_overlay(_cfg(), kind="hourly")
  assert not skip_soft_rally_entry_overlay(_cfg(), kind="hourly_trial")


def test_prefer_passive_below_edge():
  pricing = LiveEntryPricingConfig(cross_spread_enabled=True)
  pick = {"ask_edge_cents": 8}
  out = apply_align_entry_pricing(pricing, pick, cfg=_cfg(), kind="hourly", mode="live")
  assert out.cross_spread_enabled is False
  pick2 = {"ask_edge_cents": 20}
  out2 = apply_align_entry_pricing(pricing, pick2, cfg=_cfg(), kind="hourly", mode="live")
  assert out2.cross_spread_enabled is True


def test_align_disabled_by_default():
  acfg = HourlyLiveTrialAlignConfig.from_cfg({}, kind="hourly")
  assert acfg.enabled is False
