"""Tests for human live Kalshi fill reconciliation."""

from __future__ import annotations

from pathlib import Path

from src.trading.human_kalshi_sync import (
  aggregate_yes_round_trips,
  rebuild_human_live_event_from_kalshi,
)
from src.trading.human_trade_store import HumanTradeStore


def _fill(ticker, action, side, count, yes, fee=0.0, **extra):
  return {
    "ticker": ticker,
    "market_ticker": ticker,
    "action": action,
    "side": side,
    "outcome_side": side,
    "count_fp": str(count),
    "yes_price_dollars": str(yes),
    "fee_cost": str(fee),
    **extra,
  }


def test_aggregate_matches_kalshi_style_sell_yes_as_side_no():
  t = "KXBTCD-26JUL1809-T63999.99"
  fills = [
    _fill(t, "buy", "yes", 15.7, 0.62, fee=0.26),
    _fill(t, "sell", "no", 15.7, 0.90, fee=0.10),  # sell YES inventory notation
  ]
  legs = aggregate_yes_round_trips(fills, event_ticker="KXBTCD-26JUL1809")
  assert t in legs
  assert legs[t]["pnl_usd"] == 4.04
  assert legs[t]["return_pct"] == 41.5 or abs(legs[t]["return_pct"] - 41.5) < 1.0


def test_rebuild_replaces_wrong_settlement(tmp_path: Path):
  store = HumanTradeStore(tmp_path / "h.db")
  evt = "KXBTCD-26JUL1809"
  ticker = "KXBTCD-26JUL1809-T64099.99"
  pid = "pos-1"
  store.open_position({
    "id": pid,
    "event_ticker": evt,
    "market_ticker": ticker,
    "side": "yes",
    "contracts": 5,
    "entry_price_cents": 48,
    "cost_usd": 2.4,
    "label": "$64,100 or above",
    "mode": "live",
  })
  store.close_position(pid)
  store.log_trade({
    "event_ticker": evt,
    "action": "enter",
    "mode": "live",
    "market_ticker": ticker,
    "side": "yes",
    "contracts": 5,
    "entry_price_cents": 48,
    "cost_usd": 2.4,
    "position_id": pid,
    "status": "filled",
  })
  store.log_trade({
    "event_ticker": evt,
    "action": "exit",
    "mode": "live",
    "market_ticker": ticker,
    "side": "yes",
    "contracts": 5,
    "entry_price_cents": 48,
    "exit_price_cents": 100,
    "cost_usd": 2.4,
    "pnl_usd": 2.6,
    "position_id": pid,
    "status": "filled",
    "detail": "LIVE EXIT (HOUR SETTLEMENT): YES ×5 @ 100¢",
  })

  class FakeKalshi:
    authenticated = True

    def list_fills(self, limit=500):
      return [
        _fill(ticker, "buy", "yes", 101.13, 0.211, fee=1.15),
        _fill(ticker, "sell", "no", 101.13, 0.44, fee=1.74),
      ]

    def get_market_ticker(self, ticker):
      return {"title": "$64,100 or above"}

  out = rebuild_human_live_event_from_kalshi(
    store, kalshi=FakeKalshi(), event_ticker=evt,
  )
  assert out["ok"] is True
  assert abs(out["pnl_usd"] - 20.28) < 0.05
  exits = [
    t for t in store.list_trades(limit=50, event_ticker=evt)
    if t["action"] == "exit" and t["mode"] == "live"
  ]
  assert len(exits) == 1
  assert "HOUR SETTLEMENT" not in exits[0]["detail"]
  assert "Kalshi sync" in exits[0]["detail"]
  assert abs(float(exits[0]["pnl_usd"]) - 20.28) < 0.05
