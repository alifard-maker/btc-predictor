"""Tests for quick-exit overlays, hold floors, and live soft_rally wiring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.backtest.mechanics_profiles import apply_entry_profile_overlays
from src.trading.bot_live_exit import (
  allow_live_cut_loss,
  effective_min_hold_seconds,
  overlay_live_profit_settings,
  quick_exit_applies,
)
from src.trading.hourly_bot_store import HourlyBotSettings
from src.trading.live_regime_adaptive import adaptive_passive_config


def _cfg_with_quick_exit() -> dict:
  return {
    "hourly": {
      "bot": {
        "min_hold_seconds": 90,
        "hold_overlays": {
          "defense_min_hold_seconds": 30,
          "rally_min_hold_seconds": 90,
        },
        "quick_exit": {
          "enabled": True,
          "min_hold_seconds": 30,
          "cut_loss_min_hold_seconds": 30,
          "cut_loss_min_usd": 0.12,
          "take_profit_pct": 0.06,
          "take_profit_usd": 0.06,
          "apply_when": {"adaptive_mode": "defense"},
        },
      }
    }
  }


def test_quick_exit_applies_in_defense_mode():
  cfg = _cfg_with_quick_exit()
  assert quick_exit_applies(cfg, adaptive_mode="defense") is True
  assert quick_exit_applies(cfg, adaptive_mode="rally") is False


def test_effective_min_hold_seconds_mode_aware():
  cfg = _cfg_with_quick_exit()
  assert effective_min_hold_seconds(90, cfg, adaptive_mode="defense") == 30
  assert effective_min_hold_seconds(90, cfg, adaptive_mode="rally") == 90
  assert effective_min_hold_seconds(90, cfg, hour_momentum_state="pressing") == 90


def test_overlay_live_profit_settings_quick_exit_in_paper():
  settings = HourlyBotSettings(min_hold_seconds=90, take_profit_pct=0.25)
  cfg = _cfg_with_quick_exit()
  out = overlay_live_profit_settings(
    settings,
    {"entry_price_cents": 45},
    cfg,
    mode="paper",
    adaptive_mode="defense",
  )
  assert out.min_hold_seconds == 30
  assert out.take_profit_pct == 0.0
  assert out.take_profit_usd == 0.06
  assert out.take_profit_either_threshold is True


def test_allow_live_cut_loss_quick_exit_shorter_hold():
  cfg = _cfg_with_quick_exit()
  young = {
    "opened_at": datetime.now(timezone.utc).isoformat(),
    "entry_price_cents": 50,
  }
  assert not allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=-0.20,
    pos=young,
    settings_min_hold=90,
    cfg=cfg,
    kind="hourly",
    adaptive_mode="defense",
  )
  aged = {
    "opened_at": (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),
    "entry_price_cents": 50,
  }
  assert allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=-0.20,
    pos=aged,
    settings_min_hold=90,
    cfg=cfg,
    kind="hourly",
    adaptive_mode="defense",
  )


def test_allow_live_cut_loss_quick_exit_overrides_adopted_leg_hold():
  cfg = _cfg_with_quick_exit()
  cfg["hourly"]["bot"]["live_exit"] = {
    "adopted_leg_cut_loss_min_hold_seconds": 300,
    "adopted_leg_cut_loss_min_usd": 0.50,
  }
  pos = {
    "opened_at": (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),
    "entry_source": "adopted_resting",
    "entry_price_cents": 50,
  }
  assert allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=-0.20,
    pos=pos,
    settings_min_hold=90,
    cfg=cfg,
    kind="hourly",
    adaptive_mode="defense",
  )


def test_apply_entry_profile_overlays_soft_rally_on_live_hourly():
  cfg = {
    "hourly": {
      "bot": {
        "live_adaptive": {"enabled": True, "defense_threshold_only": False},
        "soft_rally": {
          "enabled": True,
          "defense_threshold_only": True,
          "defense_min_ask_edge_cents": 15.0,
          "defense_yes_mid_min_cents": 40,
          "defense_yes_mid_max_cents": 80,
        },
      }
    }
  }
  out = apply_entry_profile_overlays(cfg, kind="hourly")
  acfg = adaptive_passive_config(out)
  assert acfg.defense_threshold_only is True
  assert acfg.defense_min_ask_edge_cents == 15.0
  assert acfg.defense_yes_mid_min_cents == 40


def test_apply_entry_profile_overlays_skips_trial_soft_kind():
  cfg = {
    "hourly": {
      "bot": {
        "live_adaptive": {"defense_threshold_only": False},
        "soft_rally": {"enabled": True, "defense_threshold_only": True},
      }
    }
  }
  out = apply_entry_profile_overlays(cfg, kind="hourly_trial_soft")
  assert adaptive_passive_config(out).defense_threshold_only is False
