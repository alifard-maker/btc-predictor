"""Tests for Kalshi fill backfill into bot trade log."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.kalshi_fill_sync import (
  _aggregate_fills_to_orders,
  _aggregate_settlements_to_exits,
  _build_order_direction_cache,
  backfill_kalshi_hourly_fills,
  replay_closed_legs_from_kalshi_fills,
  summarize_kalshi_experiment_fills,
  sync_kalshi_fills_to_store,
)


def _kalshi_with_fills(fills, settlements=None):
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_fills.return_value = fills
  kalshi.list_settlements.return_value = settlements if settlements is not None else []
  kalshi.get.return_value = {"orders": []}
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
    out = replay_closed_legs_from_kalshi_fills(store, kalshi, hours=9999)
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


def test_aggregate_v2_fills_with_order_cache():
  fills = [
    {
      "order_id": "buy-v2",
      "market_ticker": "KXBTCD-26JUL0212-T60700",
      "outcome_side": "yes",
      "book_side": "bid",
      "yes_price_dollars": "0.4400",
      "count_fp": "2.00",
      "created_time": "2026-07-02T16:44:00+00:00",
    },
  ]
  cache = {"buy-v2": ("buy", "yes")}
  orders = _aggregate_fills_to_orders(fills, order_cache=cache)
  assert len(orders) == 1
  assert orders[0]["action"] == "buy"
  assert orders[0]["price_cents"] == 44


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
    out = replay_closed_legs_from_kalshi_fills(store, kalshi, hours=9999)
    assert out["ok"] is True
    assert len(out["changes"]) == 1
    trades = store.list_trades(limit=50)
    assert len([t for t in trades if t.get("action") == "enter" and t.get("status") == "filled"]) == 1
    assert len([t for t in trades if t.get("action") == "exit" and t.get("status") == "filled"]) == 1


def test_replay_repairs_scratch_exit_when_enter_already_imported():
  ticker = "KXBTCD-26JUL0413-T62599.99"
  event = "KXBTCD-26JUL0413"
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    pid = "pos-1"
    store.log_trade({
      "event_ticker": event,
      "action": "enter",
      "mode": "live",
      "market_ticker": ticker,
      "side": "yes",
      "contracts": 2,
      "price_cents": 44,
      "entry_price_cents": 44,
      "status": "filled",
      "kalshi_order_id": "buy-known",
      "position_id": pid,
      "detail": "backfilled enter",
    })
    store.log_trade({
      "event_ticker": event,
      "action": "exit",
      "mode": "live",
      "market_ticker": ticker,
      "side": "yes",
      "contracts": 2,
      "price_cents": 44,
      "entry_price_cents": 44,
      "exit_price_cents": 44,
      "pnl_usd": 0.0,
      "status": "reconciled",
      "position_id": pid,
      "detail": "Live EXIT reconciled (no Kalshi inventory)",
    })
    fills = [
      {
        "order_id": "buy-known",
        "ticker": ticker,
        "action": "buy",
        "side": "yes",
        "yes_price": 44,
        "count": 2,
        "created_time": "2026-07-04T17:05:00+00:00",
      },
      {
        "order_id": "sell-known",
        "ticker": ticker,
        "action": "sell",
        "side": "yes",
        "yes_price": 53,
        "count": 2,
        "created_time": "2026-07-04T17:40:00+00:00",
      },
    ]
    kalshi = _kalshi_with_fills(fills)
    out = replay_closed_legs_from_kalshi_fills(store, kalshi, hours=9999)
    assert any(c.get("action") == "repaired_scratch_exit" for c in out["changes"])
    exits = [
      t for t in store.list_trades(limit=20, event_ticker=event)
      if t.get("action") == "exit"
    ]
    repaired = [t for t in exits if t.get("kalshi_order_id") == "sell-known"]
    assert len(repaired) == 1
    assert float(repaired[0]["pnl_usd"]) == 0.18
    assert repaired[0]["status"] == "filled"


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
    out = backfill_kalshi_hourly_fills(store, kalshi, force=True, hours=9999)
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
    out = backfill_kalshi_hourly_fills(store, kalshi, force=True, hours=9999)
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


def test_summarize_v2_fills_without_order_cache():
  """Production Kalshi V2 fills omit legacy action/side — must pair via book_side."""
  ticker = "KXBTCD-26JUL0413-T62599.99"
  fills = [
    {
      "order_id": "buy-v2",
      "market_ticker": ticker,
      "outcome_side": "yes",
      "book_side": "bid",
      "yes_price_dollars": "0.4400",
      "count_fp": "2.00",
      "created_time": "2026-07-04T17:05:00+00:00",
    },
    {
      "order_id": "sell-v2",
      "market_ticker": ticker,
      "outcome_side": "yes",
      "book_side": "ask",
      "yes_price_dollars": "0.5300",
      "count_fp": "2.00",
      "created_time": "2026-07-04T17:40:00+00:00",
    },
  ]
  kalshi = _kalshi_with_fills(fills)
  since = datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc)
  sm = summarize_kalshi_experiment_fills(kalshi, since=since, asset="btc")
  assert sm["ok"] is True
  assert sm["fills_scanned"] == 2
  assert sm["closed_trades"] == 1
  assert sm["total_pnl_usd"] == 0.18


def test_summarize_skips_unpaired_buy_and_pairs_later_buy():
  """An earlier buy with no later sell must not block pairing for later round-trips."""
  ticker = "KXBTCD-26JUL0415-T63099.99"
  fills = [
    {
      "order_id": "open-buy",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 40,
      "count": 1,
      "created_time": "2026-07-04T18:05:00+00:00",
    },
    {
      "order_id": "closed-buy",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 44,
      "count": 2,
      "created_time": "2026-07-04T18:20:00+00:00",
    },
    {
      "order_id": "closed-sell",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price": 53,
      "count": 2,
      "created_time": "2026-07-04T18:40:00+00:00",
    },
  ]
  kalshi = _kalshi_with_fills(fills)
  since = datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc)
  sm = summarize_kalshi_experiment_fills(kalshi, since=since, asset="btc")
  # FIFO: sell closes the first open lot (open-buy), not the later closed-buy.
  assert sm["closed_trades"] == 1
  assert sm["total_pnl_usd"] == 0.13


def test_summarize_continue_after_buy_without_sell():
  ticker = "KXBTCD-26JUL0416-T62599.99"
  fills = [
    {
      "order_id": "early-sell",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price": 50,
      "count": 1,
      "created_time": "2026-07-04T19:10:00+00:00",
    },
    {
      "order_id": "blocked-buy",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 40,
      "count": 1,
      "created_time": "2026-07-04T19:30:00+00:00",
    },
    {
      "order_id": "closed-buy",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 44,
      "count": 2,
      "created_time": "2026-07-04T19:40:00+00:00",
    },
    {
      "order_id": "closed-sell",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price": 53,
      "count": 2,
      "created_time": "2026-07-04T19:50:00+00:00",
    },
  ]
  kalshi = _kalshi_with_fills(fills)
  since = datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc)
  sm = summarize_kalshi_experiment_fills(kalshi, since=since, asset="btc")
  assert sm["closed_trades"] == 1
  # blocked-buy steals the only post-epoch sell via FIFO when it exists after both buys.
  assert sm["total_pnl_usd"] == 0.13


def test_summarize_epoch_aware_pairing_skips_pre_epoch_buys():
  """Pre-epoch buys must consume post-epoch sells without mis-pairing new entries."""
  ticker = "KXBTCD-26JUL0413-T62599.99"
  fills = [
    {
      "order_id": "pre-buy",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 42,
      "count": 2,
      "created_time": "2026-07-04T16:30:00+00:00",
    },
    {
      "order_id": "post-sell-pre",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price": 48,
      "count": 2,
      "created_time": "2026-07-04T17:10:00+00:00",
    },
    {
      "order_id": "post-buy",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 44,
      "count": 2,
      "created_time": "2026-07-04T17:15:00+00:00",
    },
    {
      "order_id": "post-sell",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price": 53,
      "count": 2,
      "created_time": "2026-07-04T17:40:00+00:00",
    },
  ]
  kalshi = _kalshi_with_fills(fills)
  since = datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc)
  sm = summarize_kalshi_experiment_fills(kalshi, since=since, asset="btc")
  assert sm["ok"] is True
  assert sm["fills_scanned"] == 3
  assert sm["closed_trades"] == 1
  assert sm["total_pnl_usd"] == 0.18


def test_aggregate_v2_fills_without_order_cache():
  fills = [
    {
      "order_id": "buy-v2",
      "market_ticker": "KXBTCD-26JUL0212-T60700",
      "outcome_side": "yes",
      "book_side": "bid",
      "yes_price_dollars": "0.4400",
      "count_fp": "2.00",
      "created_time": "2026-07-02T16:44:00+00:00",
    },
  ]
  orders = _aggregate_fills_to_orders(fills, order_cache={})
  assert len(orders) == 1
  assert orders[0]["action"] == "buy"
  assert orders[0]["side"] == "yes"
  assert orders[0]["price_cents"] == 44


def test_aggregate_settlements_to_exits_yes_loss():
  ticker = "KXBTCD-26JUL0413-T62599.99"
  settlements = [{
    "ticker": ticker,
    "event_ticker": "KXBTCD-26JUL0413",
    "market_result": "no",
    "yes_count_fp": "2.00",
    "no_count_fp": "0.00",
    "value": 0,
    "settled_time": "2026-07-04T18:00:00+00:00",
  }]
  exits = _aggregate_settlements_to_exits(settlements, asset="btc")
  assert len(exits) == 1
  assert exits[0]["ticker"] == ticker
  assert exits[0]["side"] == "yes"
  assert exits[0]["action"] == "sell"
  assert exits[0]["price_cents"] == 0
  assert exits[0]["contracts"] == 2.0


def test_summarize_pairs_buy_with_settlement_exit():
  """Hourly legs held to expiry exit via /portfolio/settlements, not sell fills."""
  ticker = "KXBTCD-26JUL0413-T62599.99"
  fills = [{
    "order_id": "buy-settle",
    "ticker": ticker,
    "action": "buy",
    "side": "yes",
    "yes_price": 44,
    "count": 2,
    "created_time": "2026-07-04T17:05:00+00:00",
  }]
  settlements = [{
    "ticker": ticker,
    "event_ticker": "KXBTCD-26JUL0413",
    "market_result": "no",
    "yes_count_fp": "2.00",
    "no_count_fp": "0.00",
    "value": 0,
    "settled_time": "2026-07-04T18:00:00+00:00",
  }]
  kalshi = _kalshi_with_fills(fills, settlements=settlements)
  since = datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc)
  sm = summarize_kalshi_experiment_fills(kalshi, since=since, asset="btc")
  assert sm["ok"] is True
  assert sm["closed_trades"] == 1
  assert sm["post_epoch_settlements"] == 1
  assert sm["post_epoch_sells"] == 0
  assert sm["total_pnl_usd"] == -0.88
  assert sm["losses"] == 1


def test_summarize_settlement_winning_no_leg():
  ticker = "KXBTCD-26JUL0415-T63099.99"
  fills = [{
    "order_id": "buy-no",
    "ticker": ticker,
    "action": "buy",
    "side": "no",
    "no_price": 56,
    "count": 2,
    "created_time": "2026-07-04T18:05:00+00:00",
  }]
  settlements = [{
    "ticker": ticker,
    "event_ticker": "KXBTCD-26JUL0415",
    "market_result": "no",
    "yes_count_fp": "0.00",
    "no_count_fp": "2.00",
    "value": 0,
    "settled_time": "2026-07-04T19:00:00+00:00",
  }]
  kalshi = _kalshi_with_fills(fills, settlements=settlements)
  since = datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc)
  sm = summarize_kalshi_experiment_fills(kalshi, since=since, asset="btc")
  assert sm["closed_trades"] == 1
  assert sm["total_pnl_usd"] == 0.88
  assert sm["wins"] == 1


def test_summarize_prefers_sell_fill_over_settlement():
  """When both sell fill and settlement exist, FIFO uses the earlier exit."""
  ticker = "KXBTCD-26JUL0416-T62599.99"
  fills = [
    {
      "order_id": "buy-a",
      "ticker": ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 40,
      "count": 2,
      "created_time": "2026-07-04T19:30:00+00:00",
    },
    {
      "order_id": "sell-a",
      "ticker": ticker,
      "action": "sell",
      "side": "yes",
      "yes_price": 53,
      "count": 2,
      "created_time": "2026-07-04T19:45:00+00:00",
    },
  ]
  settlements = [{
    "ticker": ticker,
    "event_ticker": "KXBTCD-26JUL0416",
    "market_result": "no",
    "yes_count_fp": "2.00",
    "no_count_fp": "0.00",
    "value": 0,
    "settled_time": "2026-07-04T20:00:00+00:00",
  }]
  kalshi = _kalshi_with_fills(fills, settlements=settlements)
  since = datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc)
  sm = summarize_kalshi_experiment_fills(kalshi, since=since, asset="btc")
  assert sm["closed_trades"] == 1
  assert sm["total_pnl_usd"] == 0.26


def test_fill_action_side_prefers_order_cache_over_v2_sell_no():
  """V2 fills may show sell/no when the order was sell/yes (YES exit)."""
  from src.trading.kalshi_fill_sync import _fill_action_side

  fill = {
    "order_id": "exit-yes",
    "ticker": "KXETHD-26JUL1304-T1779.99",
    "action": "sell",
    "side": "no",
    "no_price": 36,
    "count_fp": "2.00",
  }
  cache = {"exit-yes": ("sell", "yes")}
  leg = _fill_action_side(fill, cache)
  assert leg == ("KXETHD-26JUL1304-T1779.99", "sell", "yes")


def test_pair_fifo_skips_settlement_after_early_sell():
  """Do not double-count settlement when inventory was already sold."""
  from datetime import timezone

  from src.trading.kalshi_fill_sync import pair_fifo_closed_legs

  t0 = datetime(2026, 7, 13, 3, 19, 22, tzinfo=timezone.utc)
  t1 = datetime(2026, 7, 13, 3, 25, 33, tzinfo=timezone.utc)
  t2 = datetime(2026, 7, 13, 4, 2, 19, tzinfo=timezone.utc)
  buys = [{
    "order_id": "buy",
    "contracts": 2.0,
    "price_cents": 80,
    "created_at": t0,
  }]
  exits = [
    {
      "order_id": "sell",
      "contracts": 2.0,
      "price_cents": 92,
      "created_at": t1,
    },
    {
      "order_id": "settle",
      "contracts": 2.0,
      "price_cents": 0,
      "created_at": t2,
      "exit_source": "settlement",
    },
  ]
  closed = pair_fifo_closed_legs(buys, exits)
  assert len(closed) == 1
  assert closed[0]["exit_type"] == "SELL"
  assert closed[0]["pnl_usd"] == 0.24
