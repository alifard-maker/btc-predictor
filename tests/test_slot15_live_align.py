"""ETH 15m config merge and live-trial align for slot15."""

from __future__ import annotations

from src.assets import asset_cfg
from src.config import load_config
from src.trading.hourly_live_trial_align import (
  HourlyLiveTrialAlignConfig,
  apply_align_entry_pricing,
  should_use_trial_leg_exits,
)
from src.trading.live_entry_price import live_entry_pricing_from_cfg


def test_eth_asset_cfg_merges_intra_slot_bot():
  cfg = load_config()
  eth = asset_cfg(cfg, "eth")
  bot = eth["intra_slot"]["bot"]
  assert bot.get("max_spend_per_slot_usd") == 25
  assert bot.get("leg_stop_loss_cents") == 4
  assert bot.get("leg_stop_gate_min_remaining_seconds") == 60


def test_intra_slot_live_trial_align_enabled():
  cfg = load_config()
  align = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind="slot15")
  assert align.enabled is True
  assert align.live_exit_mode == "trial_legs"
  assert align.mirror_trial_entry_execution is True


def test_slot15_align_re_enables_cross_spread_for_live():
  cfg = load_config()
  pricing = live_entry_pricing_from_cfg(cfg, kind="slot15", aggressive=False)
  assert pricing.cross_spread_enabled is False
  pick = {"edge": 0.15, "yes_bid": 40, "yes_ask": 42, "model_prob": 0.55}
  aligned = apply_align_entry_pricing(
    pricing, pick, cfg=cfg, kind="slot15", mode="live",
  )
  assert aligned.cross_spread_enabled is True


def test_slot15_live_uses_trial_leg_exits_when_aligned():
  cfg = load_config()
  assert should_use_trial_leg_exits(
    cfg,
    kind="slot15",
    mode="live",
    hold_seconds=30.0,
    adaptive_mode=None,
    hour_momentum_state=None,
  )
