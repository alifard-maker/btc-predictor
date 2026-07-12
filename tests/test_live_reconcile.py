"""Tests for bot vs Kalshi reconcile report."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.trading.live_reconcile import (
  build_live_reconcile_report,
  merge_kalshi_hourly_open_positions,
)


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


def test_reconcile_ignores_sports_when_asset_scoped():
  """Sports inventory on the shared Kalshi account must not flag ETH/BTC mismatch."""
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = [
    {"ticker": "KXETHD-26JUL1016-T1800", "position_fp": "2.00", "market_exposure_dollars": "1.00"},
    {"ticker": "KXLIGAMXSPREAD-26JUL18CDGTOL-CDG2", "position_fp": "6.00", "market_exposure_dollars": "2.00"},
    {"ticker": "KXLEADERMLBHR-26-JSOT", "position_fp": "10.00", "market_exposure_dollars": "0.30"},
  ]
  kalshi.list_resting_orders.return_value = []
  report = build_live_reconcile_report(
    bot_positions=[],
    kalshi=kalshi,
    asset="eth",
  )
  assert report["kalshi_legs"] == 1
  assert report["kalshi_only"][0]["ticker"].startswith("KXETH")
  assert all("LIGA" not in r["ticker"] and "LEADER" not in r["ticker"] for r in report["kalshi_only"])


def test_hourly_fill_belongs_rejects_sports():
  from src.trading.hourly_event_time import hourly_fill_belongs_to_asset

  assert hourly_fill_belongs_to_asset("KXETHD-26JUL1016-T1800", "eth")
  assert not hourly_fill_belongs_to_asset("KXLIGAMXSPREAD-26JUL18CDGTOL-CDG2", "eth")
  assert not hourly_fill_belongs_to_asset("KXLEADERMLBHR-26-JSOT", "btc")
  assert hourly_fill_belongs_to_asset("KXBTC15M-26JUL101200-T1", "btc")


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


def test_merge_open_positions_includes_eth_kxethd_sibling_range():
  from unittest.mock import patch

  bot_positions = []
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = [
    {"ticker": "KXETH-26JUL0416-B1750", "position_fp": "-2.00", "market_exposure_dollars": "1.10"},
    {"ticker": "KXETHD-26JUL0416-T1800", "position_fp": "1.00", "market_exposure_dollars": "0.86"},
  ]

  def _leg(_k, ticker, side):
    return {"contracts": 2.0, "entry_price_cents": 55, "cost_usd": 1.10}

  with patch("src.trading.live_position_sync.kalshi_position_leg", side_effect=_leg):
    merged = merge_kalshi_hourly_open_positions(
      bot_positions,
      kalshi,
      "KXETHD-26JUL0416",
      asset="eth",
    )

  assert len(merged) == 2
  assert {p["leg_strategy"] for p in merged} == {"s1_threshold", "s2_range"}


def test_reconcile_matches_kxeth_range_under_kxethd_hourly_event():
  bot_positions = [
    {
      "id": "p1",
      "mode": "live",
      "market_ticker": "KXBTC-26JUN3004-B59350",
      "side": "no",
      "contracts": 2,
      "cost_usd": 1.44,
      "label": "$59,300 to 59,399.99",
    },
    {
      "id": "p2",
      "mode": "live",
      "market_ticker": "KXBTC-26JUN3004-B59450",
      "side": "no",
      "contracts": 2,
      "cost_usd": 1.46,
      "label": "$59,400 to 59,499.99",
    },
  ]
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = [
    {"ticker": "KXBTC-26JUN3004-B59350", "position_fp": "-2.00"},
    {"ticker": "KXBTC-26JUN3004-B59450", "position_fp": "-2.00"},
    {"ticker": "WORLDCUP-T9", "position_fp": "-5.00"},
  ]
  kalshi.list_resting_orders.return_value = []
  report = build_live_reconcile_report(
    bot_positions=bot_positions,
    kalshi=kalshi,
    event_ticker="KXBTCD-26JUN3004",
  )
  assert report["ok"] is True
  assert len(report["matched"]) == 2
  assert report["kalshi_legs"] == 2


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


def test_merge_open_positions_includes_kalshi_only_s2_range():
  from unittest.mock import patch

  bot_positions = [
    {
      "id": "p1",
      "mode": "live",
      "market_ticker": "KXBTCD-26JUL0416-T63200",
      "side": "yes",
      "contracts": 1,
      "cost_usd": 0.86,
      "label": "$63,200 or above",
      "event_ticker": "KXBTCD-26JUL0416",
    }
  ]
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = [
    {"ticker": "KXBTCD-26JUL0416-T63200", "position_fp": "1.00"},
    {"ticker": "KXBTC-26JUL0416-B63200", "position_fp": "-3.00", "market_exposure_dollars": "1.67"},
    {"ticker": "KXBTC-26JUL0416-B63300", "position_fp": "3.00", "market_exposure_dollars": "1.19"},
  ]

  def _leg(_k, ticker, side):
    return {
      "contracts": 3.0,
      "entry_price_cents": 55 if "B63200" in ticker else 40,
      "cost_usd": 1.67 if "B63200" in ticker else 1.19,
    }

  with patch("src.trading.live_position_sync.kalshi_position_leg", side_effect=_leg):
    merged = merge_kalshi_hourly_open_positions(
      bot_positions,
      kalshi,
      "KXBTCD-26JUL0416",
      asset="btc",
    )

  assert len(merged) == 3
  s2 = [p for p in merged if p.get("leg_strategy") == "s2_range"]
  assert len(s2) == 2
  kalshi_only = [p for p in merged if p.get("kalshi_only")]
  assert len(kalshi_only) == 2
