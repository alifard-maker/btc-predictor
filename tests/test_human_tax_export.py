"""Human manual trade log export for live backups."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.backup.logs_backup import _export_human_log_trades, human_tax_bot_label


def test_export_human_log_trades_live(tmp_path: Path):
  data = tmp_path / "data"
  cfg = {"paths": {"logs": str(data / "logs")}}
  db = data / "eth" / "logs" / "human_trades_eth.db"
  db.parent.mkdir(parents=True)
  conn = sqlite3.connect(db)
  conn.executescript(
    """
    CREATE TABLE human_trades (
      id TEXT PRIMARY KEY,
      event_ticker TEXT,
      action TEXT,
      mode TEXT,
      market_ticker TEXT,
      side TEXT,
      contracts INTEGER,
      price_cents INTEGER,
      entry_price_cents INTEGER,
      exit_price_cents INTEGER,
      cost_usd REAL,
      pnl_usd REAL,
      signal TEXT,
      label TEXT,
      status TEXT,
      detail TEXT,
      kalshi_order_id TEXT,
      position_id TEXT,
      entry_bid_cents INTEGER,
      entry_ask_cents INTEGER,
      entry_spread_cents INTEGER,
      entry_context_json TEXT,
      created_at TEXT
    );
    INSERT INTO human_trades VALUES (
      't1', 'KXETH-1', 'enter', 'live', 'T1', 'yes', 2, 40, 40, NULL,
      0.8, NULL, 'BUY YES', 'label', 'filled', 'manual', 'oid-1', 'p1',
      39, 40, 1, '{"features":{}}', '2026-07-15T18:00:00+00:00'
    );
    """
  )
  conn.close()

  dest = tmp_path / "live"
  stats = _export_human_log_trades(cfg, mode="live", dest=dest)
  label = human_tax_bot_label("eth")
  csv_path = dest / label / "human_log_trades.csv"
  assert csv_path.exists()
  assert stats["per_bot"][label] == 1
  text = csv_path.read_text(encoding="utf-8")
  assert "entry_context_json" in text
  assert "oid-1" in text
