"""Tests for phantom period-settlement cleanup."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.trading.bot_phantom_settlement_cleanup import (
  cleanup_phantom_settlements_db,
  is_phantom_period_settlement,
)


def _make_db(path: Path) -> None:
  conn = sqlite3.connect(path)
  conn.executescript(
    """
    CREATE TABLE bot_trades (
      id TEXT PRIMARY KEY,
      event_ticker TEXT,
      market_ticker TEXT,
      trigger TEXT,
      action TEXT,
      status TEXT,
      mode TEXT,
      side TEXT,
      contracts INTEGER,
      price_cents INTEGER,
      entry_price_cents INTEGER,
      exit_price_cents INTEGER,
      pnl_usd REAL,
      detail TEXT,
      created_at TEXT
    );
    CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT);
    """
  )
  conn.close()


def test_detects_phantom_5pm_settlement_at_5am():
  row = {
    "action": "exit",
    "trigger": "period_rollover",
    "status": "filled",
    "event_ticker": "KXBTCD-26JUN3017",
    "market_ticker": "KXBTCD-26JUN3017-T59749.99",
    "detail": "Live EXIT (PERIOD SETTLEMENT): NO ×2 @ 100¢",
    "created_at": "2026-06-30T09:01:39+00:00",
    "pnl_usd": 0.62,
  }
  now = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
  assert is_phantom_period_settlement(row, now=now)


def test_allows_real_settlement_after_hour():
  row = {
    "action": "exit",
    "trigger": "period_rollover",
    "status": "filled",
    "event_ticker": "KXBTCD-26JUN3006",
    "market_ticker": "KXBTCD-26JUN3006-T59099.99",
    "detail": "Live EXIT (PERIOD SETTLEMENT): YES ×1 @ 100¢",
    "created_at": "2026-06-30T10:00:15+00:00",
    "pnl_usd": 0.1,
  }
  now = datetime(2026, 6, 30, 10, 5, tzinfo=timezone.utc)
  assert not is_phantom_period_settlement(row, now=now)


def test_cleanup_voids_phantom_rows():
  with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "hourly_bot_btc.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
      """
      INSERT INTO bot_trades (
        id, event_ticker, market_ticker, trigger, action, status, mode,
        side, contracts, exit_price_cents, entry_price_cents, pnl_usd, detail, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        "t1",
        "KXBTCD-26JUN3017",
        "KXBTCD-26JUN3017-T59749.99",
        "period_rollover",
        "exit",
        "filled",
        "live",
        "no",
        2,
        100,
        69,
        0.62,
        "Live EXIT (PERIOD SETTLEMENT): NO ×2 @ 100¢",
        "2026-06-30T09:01:39+00:00",
      ),
    )
    conn.commit()
    conn.close()

    stats = cleanup_phantom_settlements_db(db, dry_run=False)
    assert stats["voided"] == 1
    assert stats["pnl_removed_usd"] == 0.62

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT status, pnl_usd, detail FROM bot_trades WHERE id='t1'").fetchone()
    conn.close()
    assert row[0] == "voided"
    assert row[1] == 0
    assert "voided" in row[2]
