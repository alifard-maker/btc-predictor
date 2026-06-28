"""Tests for per-interval (hour/slot) profit vs loss counts."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.slot15_bot_store import Slot15BotStore


def _exit_trade(event_ticker: str, pnl_usd: float) -> dict:
  return {
    "event_ticker": event_ticker,
    "action": "exit",
    "mode": "paper",
    "status": "filled",
    "pnl_usd": pnl_usd,
  }


def _enter_trade(event_ticker: str) -> dict:
  return {
    "event_ticker": event_ticker,
    "action": "enter",
    "mode": "paper",
    "status": "filled",
    "cost_usd": 5.0,
    "contracts": 10,
    "price_cents": 50,
    "side": "yes",
  }


def test_empty_store_has_no_interval_record():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    perf = store.interval_performance("CURRENT")
    assert perf["profit_intervals"] == 0
    assert perf["loss_intervals"] == 0
    assert perf["intervals_scored"] == 0
    assert perf["current_interval"] is None


def test_completed_profit_and_loss_intervals():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade(_exit_trade("HOUR-A", 4.0))
    store.log_trade(_exit_trade("HOUR-B", -2.5))
    store.log_trade(_exit_trade("HOUR-C", 0.0))
    store.log_trade(_exit_trade("HOUR-D", 1.0))

    perf = store.interval_performance("HOUR-D")
    assert perf["profit_intervals"] == 1
    assert perf["loss_intervals"] == 1
    assert perf["breakeven_intervals"] == 1
    assert perf["intervals_scored"] == 3
    assert perf["win_rate_pct"] == 33.3
    assert perf["net_interval_pnl_usd"] == 1.5
    assert perf["interval_profit_usd"] == 4.0
    assert perf["interval_loss_usd"] == -2.5
    assert perf["current_interval"]["event_ticker"] == "HOUR-D"
    assert perf["current_interval"]["outcome"] == "profit"


def test_current_interval_excluded_from_all_time_counts():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.log_trade(_exit_trade("SLOT-1", -3.0))
    store.log_trade(_exit_trade("SLOT-2", -1.0))

    perf = store.interval_performance("SLOT-2")
    assert perf["profit_intervals"] == 0
    assert perf["loss_intervals"] == 1
    assert perf["intervals_scored"] == 1
    assert perf["current_interval"]["outcome"] == "loss"


def test_pending_interval_not_scored():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade(_enter_trade("HOUR-OLD"))
    store.log_trade(_exit_trade("HOUR-DONE", 2.0))

    perf = store.interval_performance("HOUR-NOW")
    assert perf["profit_intervals"] == 1
    assert perf["loss_intervals"] == 0
    assert perf["intervals_pending"] == 1


def test_interval_performance_in_status():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.log_trade(_exit_trade("S-A", 1.5))
    store.log_trade(_exit_trade("S-B", -0.5))

    status = store.status("S-B")
    ip = status["interval_performance"]
    assert ip["profit_intervals"] == 1
    assert ip["loss_intervals"] == 0
    assert ip["current_interval"]["event_ticker"] == "S-B"
