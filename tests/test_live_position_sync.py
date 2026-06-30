"""Tests for Kalshi inventory sync and live exit hygiene."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.live_position_sync import (
  adopt_filled_resting_enters,
  cancel_orphan_live_sell_orders,
  cancel_resting_enter_orders_for_hourly_event,
  effective_kalshi_inventory,
  hourly_event_market_tickers_from_tab,
  kalshi_sellable_contracts,
  order_still_resting,
  reconcile_close_stale_live_leg,
  resting_exit_order_id,
  resting_sell_contracts,
  sync_live_positions_from_kalshi,
  try_live_position_exit,
  verify_kalshi_exit_fill,
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


def test_adopt_filled_resting_enter_opens_bot_leg():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "status": "resting",
      "mode": "live",
      "market_ticker": "KXBTCD-EV1-T59400",
      "side": "yes",
      "contracts": 2,
      "price_cents": 40,
      "entry_price_cents": 40,
      "kalshi_order_id": "ord-rest",
      "label": "$59,400 or above",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 2
    out = adopt_filled_resting_enters(store, kalshi, "EV1")
    open_pos = store.open_positions("EV1")
    assert len(open_pos) == 1
    assert open_pos[0]["contracts"] == 2
    assert open_pos[0]["entry_price_cents"] == 40
    assert any(c.get("action") == "adopted_resting_enter" for c in out["changes"])


def test_try_live_position_exit_reconciles_when_kalshi_flat():
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
    kalshi.list_resting_orders.return_value = []
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
    assert "reconciled" in row["detail"].lower()
    assert store.open_positions("EV1") == []


def test_sync_live_positions_reconciles_when_kalshi_inventory_zero():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 1,
      "cost_usd": 0.04,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = []
    out = sync_live_positions_from_kalshi(store, kalshi, "EV1")
    assert store.open_positions("EV1") == []
    assert any(c.get("action") == "reconciled_closed" for c in out["changes"])


def test_sync_does_not_reconcile_when_resting_exit_sell_open():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 24,
      "cost_usd": 0.96,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = [
      {
        "order_id": "sell-1",
        "action": "sell",
        "ticker": "T1",
        "side": "yes",
        "remaining_count": 4,
      }
    ]
    out = sync_live_positions_from_kalshi(store, kalshi, "EV1")
    assert len(store.open_positions("EV1")) == 1
    assert not any(c.get("action") == "reconciled_closed" for c in out["changes"])


def test_try_live_position_exit_waits_when_resting_sell_open():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 24,
      "cost_usd": 0.96,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = [
      {
        "order_id": "sell-1",
        "action": "sell",
        "ticker": "T1",
        "side": "yes",
        "remaining_count": 4,
      }
    ]
    out = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=31,
      contracts=4,
      entry_c=24,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert out is None
    assert store.open_positions("EV1") != []


def test_resting_sell_contracts_counts_remaining_sells():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_resting_orders.return_value = [
    {"action": "sell", "ticker": "T1", "side": "yes", "remaining_count": 3},
    {"action": "buy", "ticker": "T1", "side": "yes", "remaining_count": 4},
    {"action": "sell", "ticker": "T2", "side": "yes", "remaining_count": 1},
  ]
  assert resting_sell_contracts(kalshi, "T1", "yes") == 3
  assert effective_kalshi_inventory(kalshi, "T1", "yes") == 3


def test_reconcile_close_stale_live_leg_logs_and_closes():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 30,
      "cost_usd": 0.6,
      "mode": "live",
      "label": "test leg",
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "resting",
      "position_id": "p1",
      "contracts": 2,
      "entry_price_cents": 30,
      "exit_price_cents": 18,
      "price_cents": 18,
    })
    row = reconcile_close_stale_live_leg(
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
    )
    assert row["status"] == "reconciled"
    assert row["pnl_usd"] == -0.24
    assert "loss" in row["detail"].lower()
    assert store.open_positions("EV1") == []


def test_sync_live_positions_from_kalshi_updates_contracts_and_merges_duplicates():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 1,
      "entry_price_cents": 80,
      "cost_usd": 0.8,
      "mode": "live",
      "opened_at": "2026-01-01T00:00:00+00:00",
    })
    store.open_position({
      "id": "p2",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 1,
      "entry_price_cents": 80,
      "cost_usd": 0.8,
      "mode": "live",
      "opened_at": "2026-01-01T00:01:00+00:00",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = -5
    out = sync_live_positions_from_kalshi(store, kalshi, "EV1")
    open_pos = store.open_positions("EV1")
    assert len(open_pos) == 1
    assert open_pos[0]["contracts"] == 5
    assert open_pos[0]["cost_usd"] == 4.0
    assert any(c.get("action") == "synced" for c in out["changes"])


def test_verify_kalshi_exit_fill_requires_inventory_drop():
  assert verify_kalshi_exit_fill(sellable_before=5.0, sellable_after=3.0, claimed_fill=2) == 2
  assert verify_kalshi_exit_fill(sellable_before=2.0, sellable_after=2.0, claimed_fill=2) == 0
  assert verify_kalshi_exit_fill(sellable_before=None, sellable_after=0.0, claimed_fill=1) == 0


def test_try_live_position_exit_rejects_unverified_api_fill():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 36,
      "cost_usd": 0.72,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = -2
    kalshi.list_resting_orders.return_value = []
    kalshi.create_order.return_value = {
      "order": {"order_id": "sell-1", "fill_count": 2, "remaining_count": 0},
    }
    row = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=44,
      contracts=2,
      entry_c=36,
      pos_mode="live",
      pick={"signal": "BUY NO"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert row is not None
    assert row["status"] == "skipped"
    assert store.open_positions("EV1") != []


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
