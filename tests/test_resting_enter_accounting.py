"""Resting live enters are not filled entries — cancel superseded, count distinct tickers."""

from __future__ import annotations

from pathlib import Path

from src.trading.hourly_bot_store import HourlyBotStore


def _resting(store: HourlyBotStore, *, market_ticker: str, price: int, oid: str):
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL0317",
    "action": "enter",
    "mode": "live",
    "market_ticker": market_ticker,
    "side": "no",
    "status": "resting",
    "entry_price_cents": price,
    "price_cents": price,
    "cost_usd": 0,
    "kalshi_order_id": oid,
    "label": "≥ $62,500",
  })


def test_supersede_cancels_old_resting_rows(tmp_path: Path):
  store = HourlyBotStore(tmp_path / "bot.db")
  _resting(store, market_ticker="KXBTCD-T62500", price=70, oid="o1")
  _resting(store, market_ticker="KXBTCD-T62500", price=71, oid="o2")
  assert store.count_resting_live_enters("KXBTCD-26JUL0317") == 1
  n = store.cancel_resting_enter_rows(
    event_ticker="KXBTCD-26JUL0317",
    market_ticker="KXBTCD-T62500",
    reason="superseded by new limit",
  )
  assert n == 2
  assert store.count_resting_live_enters("KXBTCD-26JUL0317") == 0


def test_interval_summary_counts_filled_not_resting(tmp_path: Path):
  store = HourlyBotStore(tmp_path / "bot.db")
  _resting(store, market_ticker="KXBTCD-T62500", price=70, oid="o1")
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL0317",
    "action": "enter",
    "mode": "live",
    "market_ticker": "KXBTCD-T61999",
    "side": "yes",
    "status": "filled",
    "cost_usd": 1.72,
    "entry_price_cents": 86,
  })
  summary = store.hour_interval_summary("KXBTCD-26JUL0317", mode="live")
  assert summary["enter_count"] == 1
  assert summary["filled_enter_count_this_hour"] == 1
  assert summary["resting_enter_count"] == 1
