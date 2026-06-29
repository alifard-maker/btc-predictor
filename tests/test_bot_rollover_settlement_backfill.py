"""Tests for hourly rollover settlement backfill."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from src.trading.bot_rollover_settlement_backfill import (
  backfill_hourly_rollover_db,
  correct_rollover_settlement,
  is_market_rollover_exit,
)
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def test_is_market_rollover_exit():
  assert is_market_rollover_exit({
    "trigger": "period_rollover",
    "detail": "Paper EXIT (PERIOD ROLLOVER): NO ×14 @ 80¢ — forced close at ETH hourly end",
  })
  assert not is_market_rollover_exit({
    "trigger": "period_rollover",
    "detail": "Paper EXIT (PERIOD SETTLEMENT): NO ×14 @ 100¢",
  })


def test_correct_rollover_settlement_winning_no():
  row = {
    "action": "exit",
    "trigger": "period_rollover",
    "status": "filled",
    "side": "no",
    "contracts": 14,
    "entry_price_cents": 91,
    "exit_price_cents": 80,
    "label": "$1,610 to 1,629.99",
  }
  out = correct_rollover_settlement(row, settle_price=1577.56)
  assert out is not None
  exit_c, pnl, detail = out
  assert exit_c == 100
  assert pnl == 1.26
  assert "backfilled" in detail


def test_backfill_hourly_rollover_db_updates_trade_and_bankroll():
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    logs = root / "eth" / "logs"
    logs.mkdir(parents=True)
    db_path = logs / "hourly_bot_eth.db"
    store = HourlyBotStore(db_path)
    store.save_settings(HourlyBotSettings(mode="paper", max_spend_per_hour_usd=100.0))
    store.ensure_paper_state(100.0)
    store.log_trade({
      "id": "exit-1",
      "event_ticker": "KXETH-26JUN291200",
      "trigger": "period_rollover",
      "action": "exit",
      "mode": "paper",
      "side": "no",
      "contracts": 14,
      "price_cents": 80,
      "entry_price_cents": 91,
      "exit_price_cents": 80,
      "pnl_usd": -1.54,
      "label": "$1,610 to 1,629.99",
      "status": "filled",
      "detail": "Paper EXIT (PERIOD ROLLOVER): NO ×14 @ 80¢ (entry 91¢) — forced close at ETH hourly end",
    })
    store.ensure_paper_state(100.0)

    hourly_db = logs / "hourly_predictions.db"
    conn = sqlite3.connect(hourly_db)
    conn.executescript(
      """
      CREATE TABLE hourly_predictions (
        id INTEGER PRIMARY KEY,
        event_ticker TEXT,
        asset TEXT,
        settle_brti REAL,
        logged_at TEXT,
        settle_time TEXT,
        frequency TEXT,
        series_ticker TEXT,
        title TEXT
      );
      """
    )
    conn.execute(
      "INSERT INTO hourly_predictions (event_ticker, asset, settle_brti, logged_at, settle_time, frequency, series_ticker, title) "
      "VALUES (?, 'eth', ?, '2026-06-29', '2026-06-29', 'hourly', 'KXETH', 'test')",
      ("KXETH-26JUN291200", 1577.56),
    )
    conn.commit()
    conn.close()

    cfg = {
      "paths": {"logs": str(root / "logs")},
      "eth": {"enabled": True},
    }
    stats = backfill_hourly_rollover_db(db_path, dry_run=False, data_dir=root, cfg=cfg)
    assert stats["fixed"] == 1
    trades = store.list_trades()
    assert trades[0]["exit_price_cents"] == 100
    assert trades[0]["pnl_usd"] == 1.26
    assert "PERIOD SETTLEMENT" in trades[0]["detail"]
    paper = store.get_paper_state_dict(100.0)
    assert paper["paper_bankroll_usd"] == 101.26

    stats2 = backfill_hourly_rollover_db(db_path, dry_run=False, data_dir=root, cfg=cfg)
    assert stats2["fixed"] == 0
