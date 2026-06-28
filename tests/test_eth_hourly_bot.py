"""Tests for ETH hourly auto-bet bot (continuous)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.hourly_bot import HourlyBot, bet_qualifies, _contracts_for_budget
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def _strong_bet():
  return {
    "actionable_bet": True,
    "actionable_tone": "strong",
    "actionable_headline": "STRONG ACTIONABLE BET",
  }


def test_bet_qualifies_strong_and_actionable():
  pick = {"signal": "BUY YES"}
  strong_only = HourlyBotSettings(enabled=True, allow_strong=True, allow_actionable=False)
  assert bet_qualifies(pick, _strong_bet(), strong_only)

  actionable_only = HourlyBotSettings(enabled=True, allow_strong=False, allow_actionable=True)
  moderate = {**_strong_bet(), "actionable_tone": "moderate"}
  assert bet_qualifies(pick, moderate, actionable_only)


def test_contracts_for_budget():
  assert _contracts_for_budget(10.0, 40) == 24


def test_paper_enter_fills_open_position():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=10.0))
    bot = HourlyBot(store, asset="eth")
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXETH-TEST"},
      "live": {
        "primary_pick": {
          "ticker": "KXETH-T1",
          "signal": "BUY YES",
          "kalshi_mid": 0.40,
          "edge": 0.12,
        },
        "current_price": 2500.0,
        "terminal_mu": 2510.0,
        "regime": {"allow_trade": True, "reasons": []},
        "strategy_threshold": {"contracts": []},
        "strategy_range": {"contracts": []},
      },
      "locked": {"reference_price": 2495.0},
    }
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {"min_edge": 0.05}}})
    assert len(actions) == 1
    assert actions[0]["action"] == "enter"
    assert len(store.open_positions("KXETH-TEST")) == 1
