"""Tests for paper-bot threshold auto-tuning."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.trading.bot_auto_tuning import (
  effective_entry_strategy,
  propose_auto_tuning,
  run_auto_tune_for_store,
  trade_log_span_days,
)
from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hourly_bot_store import HourlyBotStore


def _trade(action, *, days_ago=0, pnl=None, spread=3, entry=45, pid="p1"):
  ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
  row = {
    "event_ticker": "EVT",
    "trigger": "continuous",
    "action": action,
    "mode": "paper",
    "market_ticker": "MKT",
    "side": "yes",
    "contracts": 10,
    "status": "filled",
    "position_id": pid,
    "created_at": ts,
  }
  if action == "enter":
    row.update({
      "price_cents": entry,
      "entry_price_cents": entry,
      "cost_usd": entry / 10.0,
      "entry_spread_cents": spread,
    })
  else:
    row.update({
      "entry_price_cents": entry,
      "exit_price_cents": entry + 2,
      "pnl_usd": pnl,
    })
  return row


def test_trade_log_span_days_requires_week():
  trades = [_trade("enter", days_ago=8), _trade("exit", days_ago=0, pnl=1.0)]
  assert trade_log_span_days(trades) >= 7.9


def test_effective_entry_strategy_applies_tuning():
  base = EntryStrategyConfig(min_ask_edge_cents=8, kelly_fraction=0.15)
  cfg = {"hourly": {"bot": {"entry_strategy": {"min_ask_edge_cents": 8, "kelly_fraction": 0.15}}}}
  tuned = effective_entry_strategy(cfg, kind="hourly", tuning={"active": True, "min_ask_edge_cents": 10, "kelly_fraction": 0.12})
  assert tuned.min_ask_edge_cents == 10
  assert tuned.kelly_fraction == 0.12
  assert base.min_ask_edge_cents == 8


def test_propose_raises_edge_when_losing():
  estrat = EntryStrategyConfig(min_ask_edge_cents=8, kelly_fraction=0.15)
  trades = []
  for i in range(20):
    pid = f"p{i}"
    trades.append(_trade("enter", days_ago=14 - i // 2, pid=pid))
    trades.append(_trade("exit", days_ago=14 - i // 2, pnl=-0.5 if i % 3 else 0.2, pid=pid))
  proposal = propose_auto_tuning(
    trades,
    estrat=estrat,
    tune_cfg={"min_days": 7, "min_closed_trades": 15},
    previous=None,
  )
  assert proposal["ok"]
  assert proposal["reason"] == "tuned"
  assert proposal["min_ask_edge_cents"] > 8


def test_run_auto_tune_persists_to_store():
  with tempfile.TemporaryDirectory() as td:
    store = HourlyBotStore(Path(td) / "bot.db")
    trades = []
    for i in range(18):
      pid = f"x{i}"
      trades.append(_trade("enter", days_ago=14 - i // 2, pid=pid))
      trades.append(_trade("exit", days_ago=14 - i // 2, pnl=-0.4, pid=pid))
    for t in trades:
      store.log_trade(t)
    cfg = {
      "bot_auto_tune": {"enabled": True, "min_days": 7, "min_closed_trades": 15},
      "hourly": {"bot": {"entry_strategy": {"min_ask_edge_cents": 8, "kelly_fraction": 0.15}}},
    }
    out = run_auto_tune_for_store(store, cfg=cfg, kind="hourly")
    assert out.get("active") is True
    saved = store.get_auto_tuning()
    assert saved.get("active") is True
    assert float(saved.get("min_ask_edge_cents", 0)) >= 8
