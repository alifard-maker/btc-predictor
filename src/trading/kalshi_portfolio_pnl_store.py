"""Persistent Kalshi portfolio P&L ledger (closed legs + buy entries)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kalshi_portfolio_runtime (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  stats_epoch_at TEXT,
  last_sync_at TEXT,
  clean_sheets INTEGER NOT NULL DEFAULT 0,
  first_recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS kalshi_portfolio_closed_legs (
  fingerprint TEXT PRIMARY KEY,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  category TEXT NOT NULL,
  contracts INTEGER NOT NULL,
  entry_cents INTEGER NOT NULL,
  exit_cents INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  pnl_usd REAL NOT NULL,
  buy_at TEXT NOT NULL,
  exit_at TEXT NOT NULL,
  exit_type TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kpp_closed_exit_at ON kalshi_portfolio_closed_legs(exit_at);
CREATE INDEX IF NOT EXISTS idx_kpp_closed_buy_at ON kalshi_portfolio_closed_legs(buy_at);

CREATE TABLE IF NOT EXISTS kalshi_portfolio_entries (
  fingerprint TEXT PRIMARY KEY,
  order_id TEXT,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  category TEXT NOT NULL,
  contracts INTEGER NOT NULL,
  price_cents INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  bought_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kpp_entries_bought_at ON kalshi_portfolio_entries(bought_at);

INSERT OR IGNORE INTO kalshi_portfolio_runtime (id, clean_sheets) VALUES (1, 0);
"""


def _utc_now() -> str:
  return datetime.now(timezone.utc).isoformat()


def portfolio_pnl_db_path(cfg: dict[str, Any] | None) -> Path:
  logs = Path((cfg or {}).get("paths", {}).get("logs", "data/logs"))
  return logs / "kalshi_portfolio_pnl.db"


class KalshiPortfolioPnlStore:
  def __init__(self, db_path: Path | str):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._init_db()

  def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(str(self.db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

  def _init_db(self) -> None:
    with self._connect() as conn:
      conn.executescript(_SCHEMA)

  def runtime(self) -> dict[str, Any]:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT stats_epoch_at, last_sync_at, clean_sheets, first_recorded_at "
        "FROM kalshi_portfolio_runtime WHERE id = 1"
      ).fetchone()
    if not row:
      return {
        "stats_epoch_at": None,
        "last_sync_at": None,
        "clean_sheets": 0,
        "first_recorded_at": None,
      }
    return {
      "stats_epoch_at": row["stats_epoch_at"],
      "last_sync_at": row["last_sync_at"],
      "clean_sheets": int(row["clean_sheets"] or 0),
      "first_recorded_at": row["first_recorded_at"],
    }

  def stats_epoch_at(self) -> str | None:
    raw = self.runtime().get("stats_epoch_at")
    return str(raw) if raw else None

  def set_stats_epoch_now(self) -> str:
    now = _utc_now()
    with self._connect() as conn:
      conn.execute(
        """
        UPDATE kalshi_portfolio_runtime
        SET stats_epoch_at = ?, clean_sheets = COALESCE(clean_sheets, 0) + 1
        WHERE id = 1
        """,
        (now,),
      )
    return now

  def touch_sync(self) -> None:
    now = _utc_now()
    with self._connect() as conn:
      conn.execute(
        """
        UPDATE kalshi_portfolio_runtime
        SET last_sync_at = ?,
            first_recorded_at = COALESCE(first_recorded_at, ?)
        WHERE id = 1
        """,
        (now, now),
      )

  def upsert_closed_legs(self, rows: list[dict[str, Any]]) -> int:
    return self.replace_closed_legs(rows)

  def replace_closed_legs(self, rows: list[dict[str, Any]]) -> int:
    """Replace ledger with a fresh Kalshi recompute (avoids stale mis-paired legs)."""
    now = _utc_now()
    with self._connect() as conn:
      conn.execute("DELETE FROM kalshi_portfolio_closed_legs")
      for row in rows:
        buy_at = row["buy_at"]
        exit_at = row["exit_at"]
        if isinstance(buy_at, datetime):
          buy_at = buy_at.astimezone(timezone.utc).isoformat()
        if isinstance(exit_at, datetime):
          exit_at = exit_at.astimezone(timezone.utc).isoformat()
        conn.execute(
          """
          INSERT INTO kalshi_portfolio_closed_legs (
            fingerprint, ticker, side, category, contracts, entry_cents, exit_cents,
            cost_usd, pnl_usd, buy_at, exit_at, exit_type, recorded_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          """,
          (
            str(row["fingerprint"]),
            str(row["ticker"]),
            str(row["side"]),
            str(row["category"]),
            int(row["contracts"]),
            int(row["entry_cents"]),
            int(row["exit_cents"]),
            float(row["cost_usd"]),
            float(row["pnl_usd"]),
            str(buy_at),
            str(exit_at),
            str(row["exit_type"]),
            now,
          ),
        )
    return len(rows)

  def upsert_entries(self, rows: list[dict[str, Any]]) -> int:
    return self.replace_entries(rows)

  def replace_entries(self, rows: list[dict[str, Any]]) -> int:
    """Replace buy-entry ledger from a fresh Kalshi recompute."""
    now = _utc_now()
    with self._connect() as conn:
      conn.execute("DELETE FROM kalshi_portfolio_entries")
      for row in rows:
        bought_at = row["bought_at"]
        if isinstance(bought_at, datetime):
          bought_at = bought_at.astimezone(timezone.utc).isoformat()
        conn.execute(
          """
          INSERT INTO kalshi_portfolio_entries (
            fingerprint, order_id, ticker, side, category, contracts,
            price_cents, cost_usd, bought_at, recorded_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          """,
          (
            str(row["fingerprint"]),
            str(row.get("order_id") or ""),
            str(row["ticker"]),
            str(row["side"]),
            str(row["category"]),
            int(row["contracts"]),
            int(row["price_cents"]),
            float(row["cost_usd"]),
            str(bought_at),
            now,
          ),
        )
    return len(rows)

  def list_closed_legs(self) -> list[dict[str, Any]]:
    with self._connect() as conn:
      rows = conn.execute(
        """
        SELECT ticker, side, category, contracts, entry_cents, exit_cents,
               cost_usd, pnl_usd, buy_at, exit_at, exit_type
        FROM kalshi_portfolio_closed_legs
        ORDER BY exit_at ASC
        """
      ).fetchall()
    return [self._closed_row_to_dict(r) for r in rows]

  def list_entries(self) -> list[dict[str, Any]]:
    with self._connect() as conn:
      rows = conn.execute(
        """
        SELECT order_id, ticker, side, category, contracts, price_cents,
               cost_usd, bought_at
        FROM kalshi_portfolio_entries
        ORDER BY bought_at ASC
        """
      ).fetchall()
    return [self._entry_row_to_dict(r) for r in rows]

  @staticmethod
  def _category_for_ticker(ticker: str) -> str:
    from src.trading.kalshi_portfolio_pnl import categorize_ticker

    return categorize_ticker(ticker)

  @staticmethod
  def _closed_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    buy_at = datetime.fromisoformat(str(row["buy_at"]).replace("Z", "+00:00"))
    exit_at = datetime.fromisoformat(str(row["exit_at"]).replace("Z", "+00:00"))
    return {
      "ticker": row["ticker"],
      "side": row["side"],
      "category": KalshiPortfolioPnlStore._category_for_ticker(str(row["ticker"])),
      "contracts": int(row["contracts"]),
      "entry_cents": int(row["entry_cents"]),
      "exit_cents": int(row["exit_cents"]),
      "cost_usd": float(row["cost_usd"]),
      "pnl_usd": float(row["pnl_usd"]),
      "buy_at": buy_at,
      "exit_at": exit_at,
      "exit_type": row["exit_type"],
    }

  @staticmethod
  def _entry_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    bought_at = datetime.fromisoformat(str(row["bought_at"]).replace("Z", "+00:00"))
    return {
      "order_id": row["order_id"],
      "ticker": row["ticker"],
      "side": row["side"],
      "category": KalshiPortfolioPnlStore._category_for_ticker(str(row["ticker"])),
      "contracts": int(row["contracts"]),
      "price_cents": int(row["price_cents"]),
      "cost_usd": float(row["cost_usd"]),
      "bought_at": bought_at,
    }
