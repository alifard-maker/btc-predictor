"""Range-band legs use sibling KXETH-/KXBTC- event keys — must roll into hour stats."""

from __future__ import annotations

import sqlite3

from src.trading.bot_mode_stats import interval_summary_row
from src.trading.hourly_bot_store import HourlyBotStore


def test_interval_summary_includes_range_sibling_event_ticker(tmp_path):
  logs = tmp_path / "logs"
  logs.mkdir()
  store = HourlyBotStore(logs / "hourly_bot_eth.db")
  store.log_trade({
    "event_ticker": "KXETH-26JUL0405",
    "action": "enter",
    "mode": "live",
    "market_ticker": "KXETH-26JUL0405-B17401759",
    "side": "no",
    "contracts": 25,
    "price_cents": 43,
    "cost_usd": 10.66,
    "status": "filled",
    "label": "$1,740 to $1,759.99",
  })
  store.log_trade({
    "event_ticker": "KXETHD-26JUL0405",
    "action": "exit",
    "mode": "live",
    "market_ticker": "KXETH-26JUL0405-B17401759",
    "side": "no",
    "contracts": 25,
    "exit_price_cents": 100,
    "pnl_usd": 14.34,
    "status": "filled",
  })
  with store._connect() as conn:
    row = interval_summary_row(conn, "KXETHD-26JUL0405", mode="live")
  assert row["enter_count"] == 1
  assert row["exit_count"] == 1
  assert float(row["realized_pnl_usd"]) == 14.34


def test_open_position_normalizes_range_event_to_threshold_parent(tmp_path):
  logs = tmp_path / "logs"
  logs.mkdir()
  store = HourlyBotStore(logs / "hourly_bot_eth.db")
  store.open_position({
    "event_ticker": "KXETH-26JUL0405",
    "market_ticker": "KXETH-26JUL0405-B17601779",
    "side": "yes",
    "contracts": 10,
    "entry_price_cents": 30,
    "cost_usd": 2.99,
    "mode": "live",
  })
  legs = store.open_positions("KXETHD-26JUL0405")
  assert len(legs) == 1
  assert legs[0]["event_ticker"] == "KXETHD-26JUL0405"
