"""Tests for paper bot performance report aggregation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.bot_performance_report import build_bot_performance_report
from src.trading.hourly_bot_store import HourlyBotStore


def _seed_round_trip(store: HourlyBotStore, *, entry: int, spread: int, pnl: float, pid: str = "p1"):
  store.log_trade({
    "id": f"{pid}-enter",
    "event_ticker": "EVT",
    "trigger": "continuous",
    "action": "enter",
    "mode": "paper",
    "market_ticker": "MKT",
    "side": "yes",
    "contracts": 10,
    "price_cents": entry,
    "entry_price_cents": entry,
    "cost_usd": entry / 100.0 * 10,
    "status": "filled",
    "position_id": pid,
    "entry_bid_cents": entry - spread,
    "entry_ask_cents": entry,
    "entry_spread_cents": spread,
    "created_at": "2026-01-01T12:00:00Z",
  })
  store.log_trade({
    "id": f"{pid}-exit",
    "event_ticker": "EVT",
    "trigger": "continuous",
    "action": "exit",
    "mode": "paper",
    "market_ticker": "MKT",
    "side": "yes",
    "contracts": 10,
    "price_cents": entry + int(pnl * 100 / 10),
    "entry_price_cents": entry,
    "exit_price_cents": entry + int(pnl * 100 / 10),
    "pnl_usd": pnl,
    "status": "filled",
    "position_id": pid,
    "created_at": "2026-01-01T12:05:00Z",
  })


def test_performance_report_buckets_and_summary():
  with tempfile.TemporaryDirectory() as td:
    store = HourlyBotStore(Path(td) / "bot.db")
    _seed_round_trip(store, entry=45, spread=3, pnl=1.20, pid="a")
    _seed_round_trip(store, entry=45, spread=3, pnl=-0.80, pid="b")
    _seed_round_trip(store, entry=72, spread=12, pnl=-1.50, pid="c")
    trades = store.list_trades(limit=100)
    report = build_bot_performance_report(kind="hourly", asset="btc", trades=trades, min_ask_edge_cents=8)
    sm = report["summary"]
    assert sm["closed_trades"] == 3
    assert sm["wins"] == 1
    assert sm["losses"] == 2
    assert sm["total_pnl_usd"] == -1.1
    mid = next(r for r in report["by_entry_price"] if r["bucket"] == "41–60¢")
    assert mid["trades"] == 2
    wide = next(r for r in report["by_spread"] if r["bucket"] == "11¢+")
    assert wide["trades"] == 1
    assert report["recommendations"]
