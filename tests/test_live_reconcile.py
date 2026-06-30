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
  assert report["bot_live_exposure_usd"] == 1.0


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


def test_reconcile_filters_kalshi_positions_to_event():
  bot_positions = [
    {
      "id": "p1",
      "mode": "live",
      "market_ticker": "EV1-T1",
      "side": "no",
      "contracts": 1,
      "cost_usd": 0.5,
      "label": "hourly",
    }
  ]
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = [
    {"ticker": "EV1-T1", "position_fp": "-1.00"},
    {"ticker": "WORLDCUP-T9", "position_fp": "-5.00"},
  ]
  kalshi.list_resting_orders.return_value = []
  report = build_live_reconcile_report(
    bot_positions=bot_positions,
    kalshi=kalshi,
    event_ticker="EV1",
  )
  assert report["ok"] is True
  assert report["kalshi_legs"] == 1
  assert report["kalshi_contracts"] == 1


def test_reconcile_filters_kalshi_positions_to_market_tickers():
  bot_positions = [
    {
      "id": "p1",
      "mode": "live",
      "market_ticker": "KXBTC15M-T1",
      "side": "yes",
      "contracts": 1,
      "cost_usd": 0.5,
      "label": "15m up",
    }
  ]
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = [
    {"ticker": "KXBTC15M-T1", "position_fp": "1.00"},
    {"ticker": "KXBTC15M-T2", "position_fp": "3.00"},
  ]
  kalshi.list_resting_orders.return_value = []
  report = build_live_reconcile_report(
    bot_positions=bot_positions,
    kalshi=kalshi,
    market_tickers={"KXBTC15M-T1"},
  )
  assert report["ok"] is True
  assert report["kalshi_legs"] == 1


def test_reconcile_flags_orphan_resting_sell_when_bot_flat():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = []
  kalshi.list_resting_orders.return_value = [
    {
      "order_id": "sell-1",
      "action": "sell",
      "ticker": "KXBTC15M-T1",
      "side": "yes",
      "remaining_count": 4,
      "yes_price": 31,
    }
  ]
  report = build_live_reconcile_report(
    bot_positions=[],
    kalshi=kalshi,
    market_tickers={"KXBTC15M-T1"},
  )
  assert report["ok"] is False
  assert len(report["orphan_resting_sells"]) == 1
