"""BTC live production profile mirrors hourly_trial_mech."""

from __future__ import annotations

from src.backtest.mechanics_profiles import (
  apply_live_production_mechanics,
  live_mechanics_profile_for_cfg,
)
from src.trading.hourly_live_trial_align import (
  live_entry_execution_mirror_active,
  live_resting_entry_guards_active,
  live_trial_align_active,
  live_trial_exit_align_active,
  should_mirror_trial_entry_execution,
  should_use_trial_leg_exits,
)


def _cfg(**overrides):
  bot = {
    "live_trial_align": {"enabled": True, "live_exit_mode": "trial_legs"},
    "live_mechanics_profile": "mechanical_fixes",
    "live_adaptive": {"enabled": True},
  }
  bot.update(overrides.get("bot") or {})
  return {"hourly": {"bot": bot}}


def test_live_mech_profile_disables_adaptive():
  out = apply_live_production_mechanics(_cfg(), kind="hourly", mode="live")
  assert out["hourly"]["bot"]["live_adaptive"]["enabled"] is False


def test_live_mech_profile_not_applied_to_paper_or_trial():
  cfg = _cfg()
  assert apply_live_production_mechanics(cfg, kind="hourly", mode="paper") is cfg
  assert apply_live_production_mechanics(cfg, kind="hourly_trial_mech", mode="live") is cfg


def test_live_mech_profile_not_applied_to_eth_without_key():
  cfg = {"hourly": {"bot": {"live_trial_align": {"enabled": True}}}}
  assert live_mechanics_profile_for_cfg(cfg) is None
  assert apply_live_production_mechanics(cfg, kind="hourly", mode="live") is cfg


def test_entry_align_off_exit_align_on_with_mech_profile():
  cfg = _cfg()
  assert not live_trial_align_active(cfg, kind="hourly", mode="live")
  assert live_entry_execution_mirror_active(cfg, kind="hourly", mode="live")
  assert should_mirror_trial_entry_execution(cfg, kind="hourly", mode="live")
  assert live_trial_exit_align_active(cfg, kind="hourly", mode="live")
  assert live_resting_entry_guards_active(cfg, kind="hourly", mode="live")
  assert should_use_trial_leg_exits(
    cfg, kind="hourly", mode="live",
    hold_seconds=30.0, adaptive_mode="defense", hour_momentum_state="normal",
  )


def test_entry_align_on_without_mech_profile():
  cfg = {"hourly": {"bot": {"live_trial_align": {"enabled": True}}}}
  assert live_trial_align_active(cfg, kind="hourly", mode="live")
  assert live_trial_exit_align_active(cfg, kind="hourly", mode="live")
