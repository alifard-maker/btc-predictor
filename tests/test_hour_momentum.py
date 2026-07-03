"""Tests for hour momentum governor."""

from __future__ import annotations

from dataclasses import replace

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hour_momentum import (
  HourMomentumContext,
  HourMomentumState,
  apply_hour_momentum_policy,
  compute_hour_momentum,
  hour_momentum_config,
  resolve_late_entry_config,
)
from src.trading.hourly_regime import late_entry_config


def _cfg(**overrides):
  base = {
    "hourly": {
      "bot": {
        "live_adaptive": {"enabled": True, "profit_lock_usd": 1.25},
        "hour_momentum": {
          "enabled": True,
          "profit_protect_pnl_usd": 0.75,
          "min_closed_wins_to_press": 1,
        },
      }
    }
  }
  if overrides:
    base["hourly"]["bot"]["hour_momentum"].update(overrides)
  return base


def _ctx(**kw):
  defaults = {
    "realized_pnl_usd": 0.0,
    "unrealized_pnl_usd": 0.0,
    "closed_wins": 0,
    "closed_losses": 0,
    "exit_count": 0,
    "adaptive_mode": "defense",
    "primary_pick_edge": 0.10,
  }
  defaults.update(kw)
  return HourMomentumContext(**defaults)


def test_disabled_returns_none():
  cfg = {"hourly": {"bot": {"hour_momentum": {"enabled": False}}}}
  assert compute_hour_momentum(_ctx(), cfg) is None


def test_conservative_on_losing_hour():
  policy = compute_hour_momentum(_ctx(realized_pnl_usd=-0.25), _cfg())
  assert policy is not None
  assert policy.state == HourMomentumState.CONSERVATIVE
  assert policy.max_entries_per_cycle == 2
  assert policy.stake_mult == 0.8
  assert policy.late_entry_min_ask_edge_cents == 18.0


def test_conservative_on_choppy_hour():
  policy = compute_hour_momentum(
    _ctx(realized_pnl_usd=0.05, closed_wins=1, closed_losses=1, exit_count=2),
    _cfg(),
  )
  assert policy is not None
  assert policy.state == HourMomentumState.CONSERVATIVE


def test_normal_default():
  policy = compute_hour_momentum(_ctx(adaptive_mode="defense"), _cfg())
  assert policy is not None
  assert policy.state == HourMomentumState.NORMAL
  assert policy.max_entries_per_cycle == 4


def test_pressing_requires_rally_and_win():
  policy = compute_hour_momentum(
    _ctx(adaptive_mode="rally", realized_pnl_usd=0.40, closed_wins=1, exit_count=1),
    _cfg(),
  )
  assert policy is not None
  assert policy.state == HourMomentumState.PRESSING
  assert policy.max_entries_per_cycle == 6
  assert policy.max_stake_per_entry_usd == 4.0
  assert policy.late_entry_min_ask_edge_cents == 12.0


def test_no_press_in_defense_even_when_winning():
  policy = compute_hour_momentum(
    _ctx(adaptive_mode="defense", realized_pnl_usd=0.40, closed_wins=2, exit_count=2),
    _cfg(),
  )
  assert policy is not None
  assert policy.state == HourMomentumState.NORMAL


def test_locked_on_profit_protect():
  policy = compute_hour_momentum(_ctx(realized_pnl_usd=0.80), _cfg())
  assert policy is not None
  assert policy.state == HourMomentumState.LOCKED
  assert policy.block_late_entry is True
  assert policy.stake_mult == 0.7


def test_apply_policy_caps_entries_and_stake():
  estrat = EntryStrategyConfig(max_entries_per_cycle=4, max_stake_per_entry_usd=3.5)
  policy = compute_hour_momentum(_ctx(realized_pnl_usd=-0.50), _cfg())
  out = apply_hour_momentum_policy(estrat, policy)
  assert out.max_entries_per_cycle == 2
  assert out.max_stake_per_entry_usd == 2.8


def test_resolve_late_entry_blocks_when_locked():
  policy = compute_hour_momentum(_ctx(realized_pnl_usd=0.80), _cfg())
  le = resolve_late_entry_config(_cfg(), policy)
  assert le.enabled is False


def test_resolve_late_entry_overrides_edge():
  policy = compute_hour_momentum(
    _ctx(adaptive_mode="rally", realized_pnl_usd=0.40, closed_wins=1, exit_count=1),
    _cfg(),
  )
  le = resolve_late_entry_config(_cfg(), policy)
  base = late_entry_config(_cfg())
  assert le.enabled is True
  assert le.min_ask_edge_cents == 12.0
  assert base.min_ask_edge_cents == 15.0


def test_profit_lock_threshold_from_live_adaptive():
  cfg = {
    "hourly": {
      "bot": {
        "live_adaptive": {"enabled": True, "profit_lock_usd": 2.0},
        "hour_momentum": {"enabled": True},
      }
    }
  }
  mcfg = hour_momentum_config(cfg)
  assert mcfg.profit_lock_threshold_usd == 2.0
  assert mcfg.profit_protect_pnl_usd == 1.2
