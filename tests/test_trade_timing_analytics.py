"""Tests for trade timing analytics."""

from __future__ import annotations

from datetime import datetime, timezone

from src.trading.trade_timing_analytics import (
  bucket_minutes_to_settle,
  build_trade_timing_report,
  minutes_to_settle_at_trade,
)


def test_bucket_minutes_to_settle():
  assert bucket_minutes_to_settle(10.0) == "10–15m left"
  assert bucket_minutes_to_settle(9.9) == "5–10m left"
  assert bucket_minutes_to_settle(None) == "unknown"


def test_minutes_from_entry_settings():
  trade = {
    "action": "enter",
    "created_at": "2026-07-06T20:00:00+00:00",
    "event_ticker": "KXBTCD-26JUL0616",
    "entry_settings": {"hours_to_settle": 10 / 60},
  }
  assert minutes_to_settle_at_trade(trade) == 10.0


def test_build_trade_timing_report_groups_by_entry_bucket():
  trades = [
    {
      "id": "e1",
      "action": "enter",
      "status": "filled",
      "mode": "live",
      "position_id": "p1",
      "event_ticker": "KXBTCD-26JUL0616",
      "created_at": "2026-07-06T19:50:00+00:00",
      "entry_settings": {"hours_to_settle": 10 / 60},
      "market_ticker": "KXBTCD-26JUL0616-T64000",
      "side": "yes",
      "signal": "BUY YES",
      "cost_usd": 0.55,
      "price_cents": 55,
      "entry_price_cents": 55,
    },
    {
      "action": "exit",
      "status": "filled",
      "mode": "live",
      "position_id": "p1",
      "event_ticker": "KXBTCD-26JUL0616",
      "created_at": "2026-07-06T19:55:00+00:00",
      "market_ticker": "KXBTCD-26JUL0616-T64000",
      "side": "yes",
      "contracts": 1,
      "entry_price_cents": 55,
      "exit_price_cents": 70,
      "pnl_usd": 0.15,
      "exit_context": {"hours_to_settle": 5 / 60, "exit_reason": "TAKE PROFIT"},
      "detail": "TAKE PROFIT",
    },
    {
      "id": "e2",
      "action": "enter",
      "status": "filled",
      "mode": "live",
      "position_id": "p2",
      "event_ticker": "KXBTCD-26JUL0615",
      "created_at": "2026-07-06T18:30:00+00:00",
      "entry_settings": {"hours_to_settle": 30 / 60},
      "market_ticker": "KXBTCD-26JUL0615-T64000",
      "side": "yes",
      "cost_usd": 0.50,
      "price_cents": 50,
      "entry_price_cents": 50,
    },
    {
      "action": "exit",
      "status": "filled",
      "mode": "live",
      "position_id": "p2",
      "event_ticker": "KXBTCD-26JUL0615",
      "created_at": "2026-07-06T18:45:00+00:00",
      "market_ticker": "KXBTCD-26JUL0615-T64000",
      "side": "yes",
      "contracts": 1,
      "entry_price_cents": 50,
      "exit_price_cents": 30,
      "pnl_usd": -0.20,
      "exit_context": {"hours_to_settle": 15 / 60, "exit_reason": "LEG STOP"},
      "detail": "LEG STOP",
    },
  ]
  since = datetime(2026, 7, 6, tzinfo=timezone.utc)
  rep = build_trade_timing_report(trades, mode="live", since=since)
  assert rep["closed_legs"] == 2
  assert rep["total_pnl_usd"] == -0.05
  by_entry = {r["bucket"]: r for r in rep["by_minutes_to_settle_at_entry"]}
  assert by_entry["10–15m left"]["total_pnl_usd"] == 0.15
  assert by_entry["30–45m left"]["total_pnl_usd"] == -0.20
  assert rep["best_leg"]["pnl_usd"] == 0.15
  assert rep["worst_leg"]["pnl_usd"] == -0.20
