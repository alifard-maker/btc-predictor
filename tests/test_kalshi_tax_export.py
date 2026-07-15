"""Tests for Kalshi wallet per-bot tax exports."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backup.kalshi_tax_export import (
  attribute_closed_leg_to_bots,
  build_live_kalshi_order_bot_map,
)


def test_attribute_fifo_leg_by_buy_order_id():
  leg = {
    "ticker": "KXETH-1",
    "side": "yes",
    "category": "ETH hourly",
    "contracts": 5,
    "entry_cents": 40,
    "exit_cents": 55,
    "cost_usd": 2.0,
    "pnl_usd": 0.75,
    "buy_at": datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
    "exit_at": datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc),
    "exit_type": "SELL",
    "buy_order_id": "order-abc",
  }
  order_map = {"order-abc": "eth_hourly"}
  rows = attribute_closed_leg_to_bots(leg, [], order_map)
  assert len(rows) == 1
  bot, row = rows[0]
  assert bot == "eth_hourly"
  assert row["pnl_usd"] == 0.75
  assert row["pnl_source"] == "kalshi_wallet"


def test_attribute_settlement_splits_by_bot_entry_cost():
  leg = {
    "ticker": "KXMLB-1",
    "side": "market",
    "category": "MLB sports",
    "contracts": 0,
    "entry_cents": 0,
    "exit_cents": 0,
    "cost_usd": 10.0,
    "pnl_usd": 2.0,
    "buy_at": datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
    "exit_at": datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
    "exit_type": "SETTLEMENT",
  }
  entries = [
    {"ticker": "KXMLB-1", "order_id": "o1", "cost_usd": 3.0},
    {"ticker": "KXMLB-1", "order_id": "o2", "cost_usd": 7.0},
  ]
  order_map = {"o1": "btc_hourly", "o2": "eth_hourly"}
  rows = attribute_closed_leg_to_bots(leg, entries, order_map)
  assert len(rows) == 2
  by_bot = {bot: row for bot, row in rows}
  assert by_bot["btc_hourly"]["cost_usd"] == 3.0
  assert by_bot["eth_hourly"]["cost_usd"] == 7.0
  assert by_bot["btc_hourly"]["pnl_usd"] == 0.6
  assert by_bot["eth_hourly"]["pnl_usd"] == 1.4


def test_attribute_category_fallback_to_primary_bot():
  leg = {
    "ticker": "KXETHD-1",
    "side": "yes",
    "category": "ETH hourly",
    "contracts": 5,
    "entry_cents": 40,
    "exit_cents": 55,
    "cost_usd": 2.0,
    "pnl_usd": 0.5,
    "buy_at": datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
    "exit_at": datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc),
    "exit_type": "SELL",
    "buy_order_id": "",
  }
  rows = attribute_closed_leg_to_bots(leg, [], {})
  assert len(rows) == 1
  assert rows[0][0] == "eth_hourly"


def test_export_scaffold_writes_per_bot_csv(tmp_path):
  from src.backup.kalshi_tax_export import export_kalshi_wallet_live_trades

  cfg = {"paths": {"logs": str(tmp_path / "data" / "logs")}}
  dest = tmp_path / "live"
  stats = export_kalshi_wallet_live_trades(cfg, None, dest)
  assert (dest / "btc_hourly" / "trades.csv").exists()
  assert (dest / "eth_hourly" / "trades.csv").exists()
  assert (dest / "btc_hourly_human" / "trades.csv").exists()
  assert (dest / "eth_hourly_human" / "trades.csv").exists()
  assert (dest / "kalshi_other" / "trades.csv").exists()
  assert (dest / "TAX_README.txt").exists()
  assert stats.get("ok") is False
  assert stats.get("reason") == "kalshi_not_authenticated"


def test_build_live_kalshi_order_bot_map_includes_human(tmp_path):
  import sqlite3

  data = tmp_path / "data"
  cfg = {"paths": {"logs": str(data / "logs")}}
  human_db = data / "logs" / "human_trades_btc.db"
  human_db.parent.mkdir(parents=True)
  conn = sqlite3.connect(human_db)
  conn.executescript(
    """
    CREATE TABLE human_trades (
      id TEXT PRIMARY KEY,
      mode TEXT,
      action TEXT,
      kalshi_order_id TEXT
    );
    INSERT INTO human_trades VALUES ('1', 'live', 'enter', 'human-order-1');
    INSERT INTO human_trades VALUES ('2', 'live', 'exit', 'human-order-2');
    INSERT INTO human_trades VALUES ('3', 'paper', 'enter', 'human-order-3');
    """
  )
  conn.close()
  mapping = build_live_kalshi_order_bot_map(cfg)
  assert mapping["human-order-1"] == "btc_hourly_human"
  assert "human-order-2" not in mapping
  assert "human-order-3" not in mapping


def test_attribute_human_order_id(tmp_path):
  leg = {
    "ticker": "KXBTCD-1",
    "side": "yes",
    "category": "BTC hourly",
    "contracts": 5,
    "entry_cents": 40,
    "exit_cents": 55,
    "cost_usd": 2.0,
    "pnl_usd": 0.75,
    "buy_at": datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
    "exit_at": datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc),
    "exit_type": "SELL",
    "buy_order_id": "human-order-1",
  }
  order_map = {"human-order-1": "btc_hourly_human"}
  rows = attribute_closed_leg_to_bots(leg, [], order_map)
  assert len(rows) == 1
  assert rows[0][0] == "btc_hourly_human"


def test_build_live_kalshi_order_bot_map(tmp_path):
  import sqlite3

  data = tmp_path / "data"
  cfg = {"paths": {"logs": str(data / "logs")}}
  db = data / "eth" / "logs" / "hourly_bot_eth.db"
  db.parent.mkdir(parents=True)
  conn = sqlite3.connect(db)
  conn.executescript(
    """
    CREATE TABLE bot_trades (
      id TEXT PRIMARY KEY,
      mode TEXT,
      kalshi_order_id TEXT
    );
    INSERT INTO bot_trades VALUES ('1', 'live', 'kalshi-1');
    INSERT INTO bot_trades VALUES ('2', 'paper', 'kalshi-2');
    """
  )
  conn.close()
  mapping = build_live_kalshi_order_bot_map(cfg)
  assert mapping["kalshi-1"] == "eth_hourly"
  assert "kalshi-2" not in mapping
