"""Tests for paper vs live mode-scoped bot statistics."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.hourly_bot_store import HourlyBotStore


def test_hour_interval_summary_filters_by_mode():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "H1",
      "action": "enter",
      "mode": "paper",
      "status": "filled",
      "cost_usd": 5.0,
      "contracts": 10,
      "price_cents": 50,
      "side": "yes",
    })
    store.log_trade({
      "event_ticker": "H1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 2.0,
    })
    store.log_trade({
      "event_ticker": "H1",
      "action": "enter",
      "mode": "live",
      "status": "filled",
      "cost_usd": 3.0,
      "contracts": 4,
      "price_cents": 75,
      "side": "no",
    })
    store.log_trade({
      "event_ticker": "H1",
      "action": "exit",
      "mode": "live",
      "status": "filled",
      "pnl_usd": -1.0,
    })

    paper = store.hour_interval_summary("H1", mode="paper")
    live = store.hour_interval_summary("H1", mode="live")

    assert paper["enter_count"] == 1
    assert paper["exit_count"] == 1
    assert paper["realized_pnl_usd"] == 2.0
    assert paper["total_entered_usd"] == 5.0

    assert live["enter_count"] == 1
    assert live["exit_count"] == 1
    assert live["realized_pnl_usd"] == -1.0
    assert live["total_entered_usd"] == 3.0


def test_interval_performance_live_only():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "H-PAPER",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 5.0,
    })
    store.log_trade({
      "event_ticker": "H-LIVE",
      "action": "exit",
      "mode": "live",
      "status": "filled",
      "pnl_usd": -2.0,
    })

    perf = store.interval_performance("H-LIVE", mode="live")
    assert perf["profit_intervals"] == 0
    assert perf["loss_intervals"] == 0
    assert perf["current_interval"]["event_ticker"] == "H-LIVE"
    assert perf["mode"] == "live"


def test_live_performance_summary_in_status():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    from src.trading.hourly_bot_store import HourlyBotSettings

    store.save_settings(HourlyBotSettings(mode="live", max_spend_per_hour_usd=10.0))
    store.log_trade({
      "event_ticker": "H1",
      "action": "enter",
      "mode": "live",
      "status": "filled",
      "cost_usd": 4.0,
      "contracts": 8,
      "price_cents": 50,
      "side": "yes",
    })
    store.log_trade({
      "event_ticker": "H1",
      "action": "exit",
      "mode": "live",
      "status": "filled",
      "pnl_usd": 1.5,
    })

    status = store.status("H1")
    lp = status["live_performance"]
    assert lp["realized_all_time_usd"] == 1.5
    assert lp["total_entered_all_time_usd"] == 4.0
    assert lp["enter_count"] == 1
    assert lp["exit_count"] == 1
    assert status["hour_summary"]["mode"] == "live"
    assert status["hour_summary"]["realized_pnl_usd"] == 1.5
