"""Tests for Kalshi inventory sync and live exit hygiene."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.live_position_sync import (
  cancel_orphan_live_sell_orders,
  cancel_resting_enter_orders_for_hourly_event,
  hourly_event_market_tickers_from_tab,
  kalshi_sellable_contracts,
  order_still_resting,
  resting_exit_order_id,
  try_live_position_exit,
)


def test_kalshi_sellable_contracts_yes_and_no():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.get_market_position.return_value = 4
  assert kalshi_sellable_contracts(kalshi, "T1", "yes") == 4
  assert kalshi_sellable_contracts(kalshi, "T1", "no") == 0

  kalshi.get_market_position.return_value = -3
  assert kalshi_sellable_contracts(kalshi, "T2", "no") == 3


def test_resting_exit_order_id_reads_latest_resting_row():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 20,
      "cost_usd": 0.4,
      "mode": "live",
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "resting",
      "position_id": "p1",
      "kalshi_order_id": "ord-9",
    })
    assert resting_exit_order_id(store, "p1") == "ord-9"


def test_order_still_resting_matches_open_orders():
  kalshi = MagicMock()
  kalshi.list_resting_orders.return_value = [{"order_id": "ord-9"}]
  assert order_still_resting(kalshi, "ord-9") is True
  assert order_still_resting(kalshi, "ord-x") is False


def test_cancel_orphan_live_sell_orders():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_resting_orders.return_value = [
    {"order_id": "a", "action": "sell", "ticker": "T-orphan"},
    {"order_id": "b", "action": "sell", "ticker": "T-open"},
    {"order_id": "c", "action": "buy", "ticker": "T-other"},
  ]
  n = cancel_orphan_live_sell_orders(kalshi, {"T-open"})
  assert n == 1
  kalshi.cancel_order.assert_called_once_with("a")


def test_hourly_event_market_tickers_from_tab_collects_contracts():
  tab = {
    "live": {
      "primary_pick": {"ticker": "KXTEST-1H-T1"},
      "strategy_threshold": {
        "best_edge": {"ticker": "KXTEST-1H-T2"},
        "contracts": [{"ticker": "KXTEST-1H-T3"}],
      },
      "strategy_range": {"most_likely": {"ticker": "KXTEST-1H-T4"}},
    }
  }
  tickers = hourly_event_market_tickers_from_tab(tab)
  assert tickers == {"KXTEST-1H-T1", "KXTEST-1H-T2", "KXTEST-1H-T3", "KXTEST-1H-T4"}


def test_cancel_resting_enter_orders_for_hourly_event_scoped():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_resting_orders.return_value = [
    {"order_id": "e1", "action": "buy", "ticker": "KXTEST-1H-T1"},
    {"order_id": "e2", "action": "buy", "ticker": "WORLDCUP-T9"},
    {"order_id": "e3", "action": "sell", "ticker": "KXTEST-1H-T1"},
  ]
  tab = {"live": {"primary_pick": {"ticker": "KXTEST-1H-T1"}}}
  n = cancel_resting_enter_orders_for_hourly_event(kalshi, "KXTEST-1H", tab)
  assert n == 1
  kalshi.cancel_order.assert_called_once_with("e1")


def test_try_live_position_exit_reconciles_phantom_inventory():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 20,
      "cost_usd": 0.8,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    row = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=25,
      contracts=4,
      entry_c=20,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="CUT LOSS",
      detail_suffix="test",
      extra_detail="",
    )
    assert row is not None
    assert row["status"] == "reconciled"
    assert store.open_positions("EV1") == []


def test_try_live_position_exit_skips_when_pending_resting_exit():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 20,
      "cost_usd": 0.8,
      "mode": "live",
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "resting",
      "position_id": "p1",
      "kalshi_order_id": "ord-1",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 4
    kalshi.list_resting_orders.return_value = [{"order_id": "ord-1"}]
    out = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=25,
      contracts=4,
      entry_c=20,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="CUT LOSS",
      detail_suffix="test",
      extra_detail="",
    )
    assert out is None
    kalshi.create_order.assert_not_called()
