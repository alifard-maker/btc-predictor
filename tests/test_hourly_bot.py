"""Tests for hourly auto-bet bot (BTC + ETH)."""

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


def test_btc_and_eth_use_separate_stores():
  with tempfile.TemporaryDirectory() as tmp:
    btc_store = HourlyBotStore(Path(tmp) / "btc.db")
    eth_store = HourlyBotStore(Path(tmp) / "eth.db")
    btc_store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=50))
    eth_store.save_settings(HourlyBotSettings(enabled=False, max_spend_per_hour_usd=10))
    assert btc_store.get_settings().max_spend_per_hour_usd == 50
    assert eth_store.get_settings().enabled is False


def test_evaluate_from_tab_delegates_to_continuous():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=10.0))
    bot = HourlyBot(store, asset="btc")
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXBTCD-TEST"},
      "live": {
        "primary_pick": {
          "ticker": "KXBTCD-T1",
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
    trade = bot.evaluate_from_tab(tab, trigger="lock_05")
    assert trade is not None
    assert trade["status"] == "filled"
    assert trade["action"] == "enter"
    assert trade["trigger"] == "continuous"
