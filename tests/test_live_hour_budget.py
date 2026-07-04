"""Live hour budget refill tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.hourly_bot import HourlyBot
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def test_live_winning_streak_extends_hour_cap_before_refill():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    max_cap = 15.0
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=max_cap,
      mode="live",
      live_auto_refill_hour_budget=True,
      use_accumulated_profit=False,
    ))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "live",
      "status": "filled",
      "cost_usd": 15.0,
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "live",
      "status": "filled",
      "pnl_usd": 5.0,
    })
    # Without win extension: remaining would be 0 (entered $15 at $15 cap).
    assert store.remaining_budget_usd("EV1", max_cap) == 5.0


def test_live_hour_budget_refill_when_interval_cap_hit():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    max_cap = 15.0
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=max_cap,
      mode="live",
      live_auto_refill_hour_budget=True,
      use_accumulated_profit=False,
    ))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "live",
      "status": "filled",
      "cost_usd": 15.0,
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "live",
      "status": "filled",
      "pnl_usd": 0.0,
    })
    assert store.remaining_budget_usd("EV1", max_cap) == 0.0

    bot = HourlyBot(store, asset="eth")
    from tests.test_hourly_bot_continuous import _live_tab

    bot.run_continuous_cycle(
      _live_tab(event="EV1"),
      cfg={"hourly": {"regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12}}},
    )
    assert store.remaining_budget_usd("EV1", max_cap) == 15.0
    budget = store.get_live_hour_budget_dict("EV1")
    assert budget["refill_count"] == 1
    assert budget["extra_budget_usd"] == 15.0


def test_live_hour_refill_disabled_keeps_fully_deployed():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    max_cap = 15.0
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=max_cap,
      mode="live",
      live_auto_refill_hour_budget=False,
    ))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "live",
      "status": "filled",
      "cost_usd": 15.0,
    })
    bot = HourlyBot(store, asset="eth")
    from tests.test_hourly_bot_continuous import _live_tab

    bot.run_continuous_cycle(
      _live_tab(event="EV1"),
      cfg={"hourly": {"regime": {"min_edge": 0.05}}},
    )
    assert store.last_skip_reason() == "fully_deployed"
    assert store.get_live_hour_budget_dict("EV1")["refill_count"] == 0
