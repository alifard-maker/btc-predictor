"""Tests for paper bot performance report aggregation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.bot_performance_report import (
  build_bot_performance_report,
  build_experiment_summary,
)
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


def test_performance_report_60d_free_mode_split():
  with tempfile.TemporaryDirectory() as td:
    store = HourlyBotStore(Path(td) / "bot.db")
    store.log_trade({
      "id": "e1",
      "event_ticker": "EVT",
      "action": "enter",
      "mode": "paper",
      "market_ticker": "MKT",
      "side": "yes",
      "contracts": 10,
      "price_cents": 45,
      "entry_price_cents": 45,
      "cost_usd": 4.5,
      "status": "filled",
      "position_id": "p1",
      "entry_settings": {"free_mode": True},
      "created_at": "2026-06-01T12:00:00Z",
    })
    store.log_trade({
      "id": "x1",
      "event_ticker": "EVT",
      "action": "exit",
      "mode": "paper",
      "market_ticker": "MKT",
      "side": "yes",
      "contracts": 10,
      "price_cents": 50,
      "entry_price_cents": 45,
      "exit_price_cents": 50,
      "pnl_usd": 0.5,
      "status": "filled",
      "position_id": "p1",
      "created_at": "2026-06-01T12:05:00Z",
    })
    trades = store.list_trades(limit=100)
    from src.trading.bot_performance_report import build_window_report

    report = build_window_report(kind="hourly", asset="btc", trades=trades, window_days=60)
    assert report["summary"]["closed_trades"] == 1
    assert "free_mode" in report["by_free_mode"]
    assert report["by_free_mode"]["free_mode"]["closed_trades"] == 1


def test_rolling_hours_report_windows():
  from datetime import datetime, timezone, timedelta

  from src.trading.bot_performance_report import (
    attach_rolling_windows,
    build_bot_performance_report,
    build_rolling_hours_report,
    is_trial_bot_kind,
    rolling_window_key,
  )

  assert rolling_window_key(1) == "last_1h"
  assert rolling_window_key(48) == "last_48h"
  assert is_trial_bot_kind("hourly_trial")
  assert is_trial_bot_kind("hourly_trial_rally")
  assert is_trial_bot_kind("hourly_trial_soft")
  assert is_trial_bot_kind("hourly_trial_mech")
  assert not is_trial_bot_kind("hourly")

  now = datetime.now(timezone.utc)
  recent = (now - timedelta(minutes=30)).isoformat()
  older = (now - timedelta(hours=2)).isoformat()
  trades = [
    {"action": "enter", "status": "filled", "cost_usd": 5, "created_at": recent, "position_id": "a"},
    {
      "action": "exit", "status": "filled", "pnl_usd": 1.0, "created_at": recent,
      "position_id": "a", "entry_price_cents": 50, "side": "yes", "contracts": 10,
    },
    {"action": "enter", "status": "filled", "cost_usd": 5, "created_at": older, "position_id": "b"},
    {
      "action": "exit", "status": "filled", "pnl_usd": -0.5, "created_at": older,
      "position_id": "b", "entry_price_cents": 50, "side": "yes", "contracts": 10,
    },
  ]
  last60 = build_rolling_hours_report(kind="hourly", asset="eth", trades=trades, window_hours=1)
  prior4 = build_rolling_hours_report(
    kind="hourly", asset="eth", trades=trades, window_hours=4, end_hours_ago=1,
  )
  assert last60["summary"]["closed_trades"] == 1
  assert last60["summary"]["total_pnl_usd"] == 1.0
  assert prior4["summary"]["closed_trades"] == 1
  assert prior4["summary"]["total_pnl_usd"] == -0.5

  with tempfile.TemporaryDirectory() as td:
    store = HourlyBotStore(Path(td) / "bot.db")
    _seed_round_trip(store, entry=45, spread=3, pnl=1.20, pid="a")
    _seed_round_trip(store, entry=45, spread=3, pnl=-0.80, pid="b")
    _seed_round_trip(store, entry=72, spread=12, pnl=-1.50, pid="c")
    trades = store.list_trades(limit=100)
    report = build_bot_performance_report(kind="hourly", asset="btc", trades=trades, min_ask_edge_cents=8)
    attach_rolling_windows(
      report, trades=trades, kind="hourly", asset="btc", min_ask_edge_cents=8,
    )
    assert "last_2h" in report["rolling_windows"]
    assert "last_48h" in report["rolling_windows"]
    assert report["last_60_min"] is report["rolling_windows"]["last_1h"]
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


def test_combined_rolling_excludes_trial_bots():
  from src.trading.bot_performance_report import (
    _combined_rolling_all,
    _combined_window_days,
    build_combined_report,
  )

  main = [
    {
      "kind": "hourly",
      "summary": {"closed_trades": 2, "total_pnl_usd": 3.0, "wins": 2},
      "last_60_days": {"summary": {"closed_trades": 2, "total_pnl_usd": 3.0, "wins": 2}},
      "rolling_windows": {
        "last_1h": {"summary": {"closed_trades": 1, "total_pnl_usd": 1.0, "wins": 1}},
      },
    },
  ]
  trial = [
    {
      "kind": "hourly_trial",
      "summary": {"closed_trades": 5, "total_pnl_usd": -10.0, "wins": 1},
      "last_60_days": {"summary": {"closed_trades": 5, "total_pnl_usd": -10.0, "wins": 1}},
      "rolling_windows": {
        "last_1h": {"summary": {"closed_trades": 2, "total_pnl_usd": -4.0, "wins": 0}},
      },
    },
  ]
  combined_main = build_combined_report(main)
  assert combined_main["total_pnl_usd"] == 3.0
  assert combined_main["closed_trades"] == 2

  rolling_main = _combined_rolling_all(main)
  assert rolling_main["last_1h"]["total_pnl_usd"] == 1.0

  rolling_trial = _combined_rolling_all(trial)
  assert rolling_trial["last_1h"]["total_pnl_usd"] == -4.0

  days_main = _combined_window_days(main, 60)
  days_trial = _combined_window_days(trial, 60)
  assert days_main["total_pnl_usd"] == 3.0
  assert days_trial["total_pnl_usd"] == -10.0


def test_build_experiment_summary_filters_by_start():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    _seed_round_trip(store, entry=50, spread=2, pnl=1.0, pid="old")
    store.log_trade({
      "event_ticker": "EVT",
      "trigger": "continuous",
      "action": "enter",
      "mode": "paper",
      "market_ticker": "MKT",
      "side": "yes",
      "contracts": 5,
      "price_cents": 50,
      "entry_price_cents": 50,
      "cost_usd": 2.5,
      "status": "filled",
      "position_id": "new",
      "created_at": "2026-07-02T08:00:00+00:00",
    })
    store.log_trade({
      "event_ticker": "EVT",
      "trigger": "continuous",
      "action": "exit",
      "mode": "paper",
      "market_ticker": "MKT",
      "side": "yes",
      "contracts": 5,
      "price_cents": 60,
      "exit_price_cents": 60,
      "pnl_usd": 0.5,
      "status": "filled",
      "position_id": "new",
      "created_at": "2026-07-02T08:05:00+00:00",
    })
    cfg = {"hourly": {"bot": {"experiment_start_at": "2026-07-02T07:20:00+00:00"}}}
    trades = store.list_trades(limit=100)
    exp = build_experiment_summary(trades, cfg=cfg, kind="hourly", asset="btc")
    assert exp is not None
    assert exp["summary"]["closed_trades"] == 1
    assert exp["summary"]["total_pnl_usd"] == 0.5


def test_build_experiment_summary_filters_live_mode_only():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    store.log_trade({
      "event_ticker": "EVT",
      "action": "enter",
      "mode": "paper",
      "market_ticker": "MKT",
      "side": "yes",
      "contracts": 5,
      "price_cents": 50,
      "entry_price_cents": 50,
      "cost_usd": 2.5,
      "status": "filled",
      "position_id": "p1",
      "created_at": "2026-07-02T08:00:00+00:00",
    })
    store.log_trade({
      "event_ticker": "EVT",
      "action": "exit",
      "mode": "paper",
      "market_ticker": "MKT",
      "side": "yes",
      "contracts": 5,
      "price_cents": 60,
      "exit_price_cents": 60,
      "pnl_usd": 0.5,
      "status": "filled",
      "position_id": "p1",
      "created_at": "2026-07-02T08:05:00+00:00",
    })
    store.log_trade({
      "event_ticker": "EVT",
      "action": "enter",
      "mode": "live",
      "market_ticker": "MKT2",
      "side": "yes",
      "contracts": 2,
      "price_cents": 44,
      "entry_price_cents": 44,
      "cost_usd": 0.88,
      "status": "filled",
      "position_id": "l1",
      "created_at": "2026-07-02T09:00:00+00:00",
    })
    store.log_trade({
      "event_ticker": "EVT",
      "action": "exit",
      "mode": "live",
      "market_ticker": "MKT2",
      "side": "yes",
      "contracts": 2,
      "price_cents": 50,
      "exit_price_cents": 50,
      "pnl_usd": 0.12,
      "status": "filled",
      "position_id": "l1",
      "created_at": "2026-07-02T09:10:00+00:00",
    })
    cfg = {"hourly": {"bot": {"experiment_start_at": "2026-07-02T07:20:00+00:00"}}}
    trades = store.list_trades(limit=100)
    exp = build_experiment_summary(
      trades, cfg=cfg, kind="hourly", asset="btc", trade_mode="live",
    )
    assert exp is not None
    assert exp["summary"]["closed_trades"] == 1
    assert exp["summary"]["total_pnl_usd"] == 0.12
