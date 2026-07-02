"""Tests for reconcile P&L only when Kalshi entry is verified."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.live_position_sync import reconcile_close_stale_live_leg


def test_reconcile_settlement_without_verified_buy_books_zero_pnl():
  ticker = "KXETHD-26JUN2923-T1589.99"
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "KXETHD-26JUN2923",
      "market_ticker": ticker,
      "side": "no",
      "contracts": 8,
      "entry_price_cents": 46,
      "cost_usd": 3.68,
      "mode": "live",
      "label": "$1,590 or above",
    })
    store.log_trade({
      "event_ticker": "KXETHD-26JUN2923",
      "action": "enter",
      "status": "filled",
      "mode": "live",
      "market_ticker": ticker,
      "side": "no",
      "contracts": 8,
      "price_cents": 46,
      "entry_price_cents": 46,
      "cost_usd": 3.68,
      "position_id": "p1",
      "kalshi_order_id": "order-no-1",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.list_fills.return_value = []
    kalshi.list_orders.return_value = []
    kalshi.get.return_value = {
      "market": {
        "status": "settled",
        "expiration_value": "1588.73",
        "strike_type": "greater",
        "floor_strike": 1589.99,
      },
    }
    row = reconcile_close_stale_live_leg(
      store=store,
      pos=store.open_positions("KXETHD-26JUN2923")[0],
      period_key="KXETHD-26JUN2923",
      kalshi=kalshi,
    )
    assert row["status"] == "reconciled"
    assert row["pnl_usd"] == 0.0
    assert "P&L not booked" in row["detail"]


def test_reconcile_settlement_with_verified_buy_books_pnl():
  ticker = "KXETHD-26JUN2923-T1589.99"
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "KXETHD-26JUN2923",
      "market_ticker": ticker,
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 46,
      "cost_usd": 0.92,
      "mode": "live",
      "label": "$1,590 or above",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.list_fills.return_value = [{
      "order_id": "buy-1",
      "ticker": ticker,
      "action": "buy",
      "side": "no",
      "yes_price": 54,
      "count": 2,
    }]
    kalshi.list_orders.return_value = []
    kalshi.get.return_value = {
      "market": {
        "status": "settled",
        "expiration_value": "1588.73",
        "strike_type": "greater",
        "floor_strike": 1589.99,
      },
    }
    row = reconcile_close_stale_live_leg(
      store=store,
      pos=store.open_positions("KXETHD-26JUN2923")[0],
      period_key="KXETHD-26JUN2923",
      kalshi=kalshi,
    )
    assert row["pnl_usd"] == 1.08
    assert "profit" in row["detail"].lower()
