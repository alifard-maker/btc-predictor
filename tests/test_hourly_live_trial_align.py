"""Tests for live hourly ↔ paper trial alignment."""

from __future__ import annotations

from src.backtest.mechanics_profiles import apply_live_production_mechanics
from src.trading.bot_live_exit import quick_exit_config
from src.trading.bot_whipsaw_guard import WhipsawGuardConfig
from src.trading.hourly_live_trial_align import (
  HourlyLiveTrialAlignConfig,
  apply_align_entry_pricing,
  apply_mirror_trial_entry_estrat,
  live_entry_stake_mirror_active,
  live_pnl_first_stake_mirror_active,
  merge_whipsaw_align_overrides,
  pending_resting_enter_blocks_entry,
  should_mirror_trial_entry_execution,
  should_mirror_trial_stake_sizing,
  should_use_trial_leg_exits,
  skip_soft_rally_entry_overlay,
)
from src.trading.entry_strategy import EntryStrategyConfig
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
          "execution": {
            "mirror_trial_entry_execution": True,
            "block_reentry_while_resting": True,
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


def test_eth_hourly_live_mirror_gets_trial_legs():
  """ETH live mirror kind must share BTC live trial-leg exits (haircut + defer)."""
  cfg = _cfg()
  cfg["hourly"]["bot"]["live_trial_align"]["live_exit_mode"] = "trial_legs"
  assert should_use_trial_leg_exits(
    cfg,
    kind="hourly_live",
    mode="live",
    hold_seconds=5000,
    adaptive_mode="rally",
    hour_momentum_state="normal",
  )
  assert not should_use_trial_leg_exits(
    cfg,
    kind="hourly_live",
    mode="paper",
    hold_seconds=5000,
    adaptive_mode="rally",
    hour_momentum_state="normal",
  )


def test_quick_exit_align_override():
  q = quick_exit_config(_cfg(), kind="hourly")
  assert q.min_hold_seconds == 60
  assert q.cut_loss_min_hold_seconds == 60


def test_whipsaw_align_override():
  w = merge_whipsaw_align_overrides(
    WhipsawGuardConfig.from_cfg(_cfg(), kind="hourly"), _cfg(), kind="hourly", mode="live",
  )
  assert w.max_quick_exit_cuts_per_hour == 2


def test_skip_soft_rally_only_for_mech_live():
  assert not skip_soft_rally_entry_overlay(_cfg(), kind="hourly")
  mech = _cfg()
  mech["hourly"]["bot"]["live_mechanics_profile"] = "mechanical_fixes"
  assert skip_soft_rally_entry_overlay(mech, kind="hourly")
  assert not skip_soft_rally_entry_overlay(_cfg(), kind="hourly_trial")


def test_prefer_passive_below_edge_when_mirror_disabled():
  cfg = _cfg()
  cfg["hourly"]["bot"]["live_trial_align"]["execution"]["mirror_trial_entry_execution"] = False
  pricing = LiveEntryPricingConfig(cross_spread_enabled=True)
  pick = {"ask_edge_cents": 8}
  out = apply_align_entry_pricing(pricing, pick, cfg=cfg, kind="hourly", mode="live")
  assert out.cross_spread_enabled is False


def test_mirror_trial_execution_enables_cross_spread():
  pricing = LiveEntryPricingConfig(cross_spread_enabled=False)
  pick = {"ask_edge_cents": 8}
  out = apply_align_entry_pricing(pricing, pick, cfg=_cfg(), kind="hourly", mode="live")
  assert out.cross_spread_enabled is True
  assert should_mirror_trial_entry_execution(_cfg(), kind="hourly", mode="live")


def test_pending_resting_blocks_entry(tmp_path):
  from unittest.mock import MagicMock

  from src.trading.hourly_bot_store import HourlyBotStore

  store = HourlyBotStore(tmp_path / "bot.db")
  store.log_trade({
    "event_ticker": "EV1",
    "market_ticker": "KX-T1",
    "action": "enter",
    "mode": "live",
    "status": "resting",
    "kalshi_order_id": "ord-1",
    "side": "no",
  })
  kalshi = MagicMock()
  kalshi.authenticated = True
  with __import__("unittest.mock", fromlist=["patch"]).patch(
    "src.trading.live_position_sync.order_still_resting",
    return_value=True,
  ):
    reason = pending_resting_enter_blocks_entry(
      store, kalshi, "EV1", "KX-T1", cfg=_cfg(), kind="hourly", mode="live",
    )
  assert reason == "pending_resting_limit:KX-T1"


def test_align_disabled_by_default():
  acfg = HourlyLiveTrialAlignConfig.from_cfg({}, kind="hourly")
  assert acfg.enabled is False
