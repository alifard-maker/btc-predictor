"""Tests for bot vs Kalshi reconcile report."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.trading.live_reconcile import build_live_reconcile_report


def test_reconcile_ok_when_bot_matches_kalshi():
  bot_positions = [
    {
      "id": "p1",
      "mode": "live",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 2,
      "cost_usd": 1.0,
      "label": "$59,400 or above",
    }
  ]
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = [{"ticker": "T1", "position_fp": "-2.00"}]
  kalshi.list_resting_orders.return_value = []
  report = build_live_reconcile_report(bot_positions=bot_positions, kalshi=kalshi)
  assert report["ok"] is True
  assert len(report["matched"]) == 1


def test_reconcile_flags_bot_only_and_orphan_sell():
  bot_positions = [
    {
      "id": "p1",
      "mode": "live",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "cost_usd": 1.0,
      "label": "range",
    }
  ]
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = []
  kalshi.get_market_position.return_value = 0
  kalshi.list_resting_orders.return_value = [
    {"order_id": "o1", "action": "sell", "ticker": "T-orphan", "side": "yes"},
  ]
  report = build_live_reconcile_report(bot_positions=bot_positions, kalshi=kalshi)
  assert report["ok"] is False
  assert len(report["bot_only"]) == 1
  assert len(report["orphan_resting_sells"]) == 1
