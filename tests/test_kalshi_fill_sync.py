"""Tests for Kalshi fill backfill into bot trade log."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.kalshi_fill_sync import (
  backfill_kalshi_hourly_fills,
  replay_closed_legs_from_kalshi_fills,
  summarize_kalshi_experiment_fills,
  sync_kalshi_fills_to_store,
)


def _kalshi_with_fills(fills):
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_fills.return_value = fills
  return kalshi


def test_backfill_closed_round_trip_from_kalshi_fills():
  ticker = "KXBTCD-26JUL0212-T60700"
  fills = [
    {
      "order_id": "buy-1",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 44,
      "count": 2,
      "created_time": "2026-07-02T16:44:00+00:00",
    },
    {
      "order_id": "sell-1",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price": 53,
      "count": 2,
      "created_time": "2026-07-02T16:45:00+00:00",
    },
  ]
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    kalshi = _kalshi_with_fills(fills)
    out = replay_closed_legs_from_kalshi_fills(store, kalshi)
    assert out["ok"] is True
    assert len(out["changes"]) == 1
    trades = store.list_trades(limit=50)
    enters = [t for t in trades if t.get("action") == "enter" and t.get("status") == "filled"]
    exits = [t for t in trades if t.get("action") == "exit" and t.get("status") == "filled"]
    assert len(enters) == 1
    assert len(exits) == 1
    assert enters[0]["kalshi_order_id"] == "buy-1"
    assert exits[0]["kalshi_order_id"] == "sell-1"
    assert float(exits[0]["pnl_usd"]) == 0.18


def test_backfill_v2_dollar_price_fills():
  ticker = "KXBTCD-26JUL0212-T60700"
  fills = [
    {
      "order_id": "buy-v2",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price_dollars": "0.4400",
      "count_fp": "2.00",
      "created_time": "2026-07-02T16:44:00+00:00",
    },
    {
      "order_id": "sell-v2",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price_dollars": "0.5300",
      "count_fp": "2.00",
      "created_time": "2026-07-02T16:45:00+00:00",
    },
  ]
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    kalshi = _kalshi_with_fills(fills)
    out = replay_closed_legs_from_kalshi_fills(store, kalshi)
    assert out["ok"] is True
    assert len(out["changes"]) == 1
    trades = store.list_trades(limit=50)
    assert len([t for t in trades if t.get("action") == "enter" and t.get("status") == "filled"]) == 1
    assert len([t for t in trades if t.get("action") == "exit" and t.get("status") == "filled"]) == 1


def test_backfill_skips_already_imported_enter():
  ticker = "KXBTCD-26JUL0212-T60700"
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "KXBTCD-26JUL0212",
      "action": "enter",
      "mode": "live",
      "market_ticker": ticker,
      "side": "yes",
      "contracts": 2,
      "price_cents": 44,
      "status": "filled",
      "kalshi_order_id": "buy-known",
      "detail": "already logged",
    })
    fills = [{
      "order_id": "buy-known",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 44,
      "count": 2,
      "created_time": "2026-07-02T16:44:00+00:00",
    }]
    kalshi = _kalshi_with_fills(fills)
    out = backfill_kalshi_hourly_fills(store, kalshi, force=True)
    assert out["changes"] == []


def test_backfill_promotes_resting_enter_on_fill():
  ticker = "KXBTCD-26JUL0201-T60700"
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "KXBTCD-26JUL0201",
      "action": "enter",
      "mode": "live",
      "market_ticker": ticker,
      "side": "yes",
      "contracts": 2,
      "price_cents": 44,
      "status": "resting",
      "kalshi_order_id": "d5d2e2ca",
      "detail": "resting",
    })
    fills = [{
      "order_id": "d5d2e2ca",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 44,
      "count": 2,
      "created_time": "2026-07-02T04:44:00+00:00",
    }]
    kalshi = _kalshi_with_fills(fills)
    out = backfill_kalshi_hourly_fills(store, kalshi, force=True)
    assert any(c.get("action") == "promoted_resting_from_fills" for c in out["changes"])
    trades = store.list_trades(limit=20, event_ticker="KXBTCD-26JUL0201")
    filled = [t for t in trades if t.get("status") == "filled" and t.get("action") == "enter"]
    assert len(filled) == 1
    assert store.open_positions("KXBTCD-26JUL0201")


def test_sync_idempotent_second_run():
  ticker = "KXBTCD-26JUL0210-T60500"
  fills = [
    {
      "order_id": "b2",
      "ticker": ticker,
      "action": "buy",
      "side": "no",
      "yes_price": 60,
      "count": 1,
      "created_time": datetime.now(timezone.utc).isoformat(),
    },
    {
      "order_id": "s2",
      "ticker": ticker,
      "action": "sell",
      "side": "no",
      "yes_price": 55,
      "count": 1,
      "created_time": datetime.now(timezone.utc).isoformat(),
    },
  ]
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    kalshi = _kalshi_with_fills(fills)
    first = sync_kalshi_fills_to_store(store, kalshi, force=True)
    second = sync_kalshi_fills_to_store(store, kalshi, force=True)
    assert len(first["changes"]) >= 1
    assert second["changes"] == []


def test_summarize_kalshi_experiment_fills_pairs_round_trips():
  ticker = "KXBTCD-26JUL0212-T60700"
  fills = [
    {
      "order_id": "buy-a",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 40,
      "count": 3,
      "created_time": "2026-07-02T10:00:00+00:00",
    },
    {
      "order_id": "sell-a",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price": 46,
      "count": 3,
      "created_time": "2026-07-02T10:30:00+00:00",
    },
  ]
  kalshi = _kalshi_with_fills(fills)
  since = datetime(2026, 7, 2, 7, 20, tzinfo=timezone.utc)
  sm = summarize_kalshi_experiment_fills(kalshi, since=since)
  assert sm["ok"] is True
  assert sm["closed_trades"] == 1
  assert sm["total_pnl_usd"] == 0.18
  assert sm["wins"] == 1
