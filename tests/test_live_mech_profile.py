"""BTC live production profile mirrors hourly_trial_mech."""

from __future__ import annotations

from src.backtest.mechanics_profiles import (
  apply_live_production_mechanics,
  live_mechanics_profile_for_cfg,
)
from src.trading.hourly_live_trial_align import (
  live_entry_stake_mirror_active,
  live_mech_paper_mirror_active,
  live_pnl_first_stake_mirror_active,
  live_resting_entry_guards_active,
  live_trial_align_active,
  live_trial_exit_align_active,
  should_mirror_trial_entry_execution,
  should_mirror_trial_stake_sizing,
  should_use_trial_leg_exits,
)
from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hourly_live_trial_align import apply_mirror_trial_entry_estrat


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


def test_pnl_first_applied_to_paper_hourly():
  cfg = {"hourly": {"bot": {"live_mechanics_profile": "pnl_first"}}}
  out = apply_live_production_mechanics(cfg, kind="hourly", mode="paper")
  assert out["hourly"]["bot"]["entry_strategy"]["min_ask_edge_cents"] == 18


def test_live_mech_profile_not_applied_to_eth_without_key():
  cfg = {"hourly": {"bot": {"live_trial_align": {"enabled": True}}}}
  assert live_mechanics_profile_for_cfg(cfg) is None
  assert apply_live_production_mechanics(cfg, kind="hourly", mode="live") is cfg


def test_entry_align_off_exit_align_on_with_mech_profile():
  cfg = _cfg()
  assert not live_trial_align_active(cfg, kind="hourly", mode="live")
  assert live_mech_paper_mirror_active(cfg, kind="hourly", mode="live")
  assert live_entry_stake_mirror_active(cfg, kind="hourly", mode="live")
  assert should_mirror_trial_entry_execution(cfg, kind="hourly", mode="live")
  assert live_trial_exit_align_active(cfg, kind="hourly", mode="live")
  assert live_resting_entry_guards_active(cfg, kind="hourly", mode="live")
  assert should_use_trial_leg_exits(
    cfg, kind="hourly", mode="live",
    hold_seconds=30.0, adaptive_mode="defense", hour_momentum_state="normal",
  )


def test_mech_mirror_restores_trial_scale_in_and_stake():
  cfg = _cfg()
  cfg["hourly"]["bot"]["entry_strategy"] = {
    "allow_scale_in": True,
    "scale_in_max_legs_per_ticker": 4,
    "max_stake_per_entry_usd": 3.5,
  }
  base = EntryStrategyConfig(max_stake_per_entry_usd=10.0, allow_scale_in=False)
  out = apply_mirror_trial_entry_estrat(base, cfg, kind="hourly", mode="live")
  assert out.allow_scale_in is True
  assert out.scale_in_max_legs_per_ticker == 4
  assert out.max_stake_per_entry_usd == 4.0


def test_entry_align_on_without_mech_profile():
  cfg = {"hourly": {"bot": {"live_trial_align": {"enabled": True}}}}
  assert live_trial_align_active(cfg, kind="hourly", mode="live")
  assert not live_mech_paper_mirror_active(cfg, kind="hourly", mode="live")
  assert live_trial_exit_align_active(cfg, kind="hourly", mode="live")


def test_pnl_first_stake_mirror_active():
  cfg = {
    "hourly": {
      "bot": {
        "live_trial_align": {"enabled": True, "live_exit_mode": "trial_legs"},
        "live_mechanics_profile": "pnl_first",
      }
    }
  }
  assert live_pnl_first_stake_mirror_active(cfg, kind="hourly", mode="live")
  assert should_mirror_trial_stake_sizing(cfg, kind="hourly", mode="live")
  assert not live_trial_align_active(cfg, kind="hourly", mode="live")
  out = apply_live_production_mechanics(cfg, kind="hourly", mode="live")
  es = out["hourly"]["bot"]["entry_strategy"]
  assert es["min_ask_edge_cents"] == 18
  assert es["max_contracts_per_entry"] == 2
  assert es["max_stake_per_entry_usd"] == 2.50
