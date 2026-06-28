"""Tests for ETH hourly auto-bet bot."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.trading.eth_hourly_bot import EthHourlyBot, bet_qualifies, _contracts_for_budget
from src.trading.eth_hourly_bot_store import EthHourlyBotSettings, EthHourlyBotStore


def _strong_bet():
  return {
    "actionable_bet": True,
    "actionable_tone": "strong",
    "actionable_headline": "STRONG ACTIONABLE BET",
  }


def _moderate_bet():
  return {
    "actionable_bet": True,
    "actionable_tone": "moderate",
    "actionable_headline": "ACTIONABLE BET",
  }


def test_bet_qualifies_strong_and_actionable():
  strong_only = EthHourlyBotSettings(enabled=True, allow_strong=True, allow_actionable=False)
  assert bet_qualifies(_strong_bet(), strong_only)
  assert not bet_qualifies(_moderate_bet(), strong_only)

  actionable_only = EthHourlyBotSettings(enabled=True, allow_strong=False, allow_actionable=True)
  assert not bet_qualifies(_strong_bet(), actionable_only)
  assert bet_qualifies(_moderate_bet(), actionable_only)


def test_contracts_for_budget():
  assert _contracts_for_budget(10.0, 40) == 24
  assert _contracts_for_budget(0.30, 40) == 0


def test_paper_bet_at_lock_05():
  with tempfile.TemporaryDirectory() as tmp:
    store = EthHourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(EthHourlyBotSettings(enabled=True, max_spend_per_hour_usd=10.0))
    bot = EthHourlyBot(store)

    tab = {
      "ok": True,
      "event": {"event_ticker": "KXETH-TEST"},
      "locked": {
        "primary_pick": {
          "ticker": "KXETH-TEST-T1",
          "signal": "BUY YES",
          "label": "$2,500+",
          "kalshi_mid": 0.40,
          "edge": 0.12,
        },
        "bet_assessment": _strong_bet(),
        "position_alert": {"alert": "HOLD", "alert_tone": "neutral"},
      },
    }
    trade = bot.evaluate_from_tab(tab, trigger="lock_05")
    assert trade is not None
    assert trade["status"] == "filled"
    assert trade["mode"] == "paper"
    assert trade["cost_usd"] == 9.6
    assert store.spent_usd("KXETH-TEST") == 9.6

    # dedupe same trigger
    assert bot.evaluate_from_tab(tab, trigger="lock_05") is None


def test_skips_cut_losses_at_lock():
  with tempfile.TemporaryDirectory() as tmp:
    store = EthHourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(EthHourlyBotSettings(enabled=True))
    bot = EthHourlyBot(store)
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXETH-TEST2"},
      "locked": {
        "primary_pick": {
          "ticker": "T1",
          "signal": "BUY NO",
          "kalshi_mid": 0.35,
        },
        "bet_assessment": _strong_bet(),
        "position_alert": {"alert": "CUT LOSSES"},
      },
    }
    trade = bot.evaluate_from_tab(tab, trigger="lock_05")
    assert trade["status"] == "skipped"


def test_intrahour_opportunity_bet():
  with tempfile.TemporaryDirectory() as tmp:
    store = EthHourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(EthHourlyBotSettings(enabled=True, max_spend_per_hour_usd=5.0))
    bot = EthHourlyBot(store)
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXETH-INTR"},
      "intrahour_opportunity": {
        "highlight": True,
        "primary_pick": {
          "ticker": "KXETH-INTR-B1",
          "signal": "BUY YES",
          "kalshi_mid": 0.25,
        },
        "bet_assessment": _strong_bet(),
      },
    }
    trade = bot.evaluate_from_tab(tab, trigger="intrahour")
    assert trade["status"] == "filled"
    assert trade["trigger"] == "intrahour"
