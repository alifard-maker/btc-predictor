"""Tests for passive vs aggressive entry presets."""

from __future__ import annotations

from src.trading.bot_entry_presets import (
  apply_bot_runtime_settings,
  effective_bot_entry_strategy,
)
from src.trading.hourly_bot_store import HourlyBotSettings
from src.trading.slot15_bot_store import Slot15BotSettings


def _pick(ticker: str, *, model_prob: float, ask: float) -> dict:
  return {
    "ticker": ticker,
    "model_prob": model_prob,
    "yes_ask": int(ask * 100),
    "yes_bid": max(1, int(ask * 100) - 2),
  }


def test_slot15_passive_default_entry_strategy():
  estrat = effective_bot_entry_strategy({}, kind="slot15", aggressive=False, tuning=None)
  assert estrat.kelly_fraction == 0.15
  assert estrat.max_budget_fraction_per_entry == 0.55
  assert estrat.max_entries_per_cycle == 1
  assert estrat.max_concurrent_positions == 1
  assert estrat.allow_scale_in is False


def test_slot15_aggressive_entry_strategy():
  estrat = effective_bot_entry_strategy({}, kind="slot15", aggressive=True, tuning=None)
  assert estrat.max_budget_fraction_per_entry == 0.10
  assert estrat.max_concurrent_positions == 6
  assert estrat.allow_scale_in is True
  assert estrat.max_entries_per_cycle == 3


def test_hourly_passive_entry_strategy():
  estrat = effective_bot_entry_strategy({}, kind="hourly", aggressive=False, tuning=None)
  assert estrat.max_entries_per_cycle == 1
  assert estrat.max_concurrent_positions == 1
  assert estrat.allow_scale_in is False
  assert estrat.allow_barbell is False
  assert estrat.max_stake_per_entry_usd == 10.0


def test_hourly_aggressive_entry_strategy():
  estrat = effective_bot_entry_strategy({}, kind="hourly", aggressive=True, tuning=None)
  assert estrat.max_budget_fraction_per_entry == 0.10
  assert estrat.max_entries_per_cycle == 3
  assert estrat.scale_in_max_legs_per_ticker == 6
  assert estrat.correlation_guard is False


def test_runtime_cooldowns_passive_vs_aggressive():
  passive = apply_bot_runtime_settings(Slot15BotSettings(), bot_kind="slot15", aggressive=False)
  aggressive = apply_bot_runtime_settings(Slot15BotSettings(), bot_kind="slot15", aggressive=True)
  assert passive.reentry_cooldown_seconds == 120
  assert passive.profit_exit_cooldown_seconds == 60
  assert aggressive.reentry_cooldown_seconds == 30
  assert aggressive.profit_exit_cooldown_seconds == 30


def test_aggressive_slot15_caps_entry_at_10_on_100_bankroll():
  from src.trading.entry_strategy import entry_budget_usd

  estrat = effective_bot_entry_strategy({}, kind="slot15", aggressive=True, tuning=None)
  pick = _pick("T", model_prob=0.80, ask=0.40)
  stake = entry_budget_usd(
    estrat=estrat,
    bankroll_usd=100.0,
    remaining_usd=100.0,
    pick=pick,
    side="yes",
  )
  assert stake == 10.0


def test_aggressive_flag_defaults_false_in_store():
  assert HourlyBotSettings().aggressive_entries is False
  assert Slot15BotSettings().aggressive_entries is False
