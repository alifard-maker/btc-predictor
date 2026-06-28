"""Persist ETH hourly bot settings and paper/live trade log."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class EthHourlyBotSettings:
  enabled: bool = False
  mode: str = "paper"  # paper | live
  max_spend_per_hour_usd: float = 25.0
  allow_strong: bool = True
  allow_actionable: bool = True

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> EthHourlyBotSettings:
    if not raw:
      return cls()
    return cls(
      enabled=bool(raw.get("enabled", False)),
      mode=str(raw.get("mode", "paper")),
      max_spend_per_hour_usd=float(raw.get("max_spend_per_hour_usd", 25.0)),
      allow_strong=bool(raw.get("allow_strong", True)),
      allow_actionable=bool(raw.get("allow_actionable", True)),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_trades (
  id TEXT PRIMARY KEY,
  event_ticker TEXT NOT NULL,
  trigger TEXT NOT NULL,
  mode TEXT NOT NULL,
  market_ticker TEXT,
  side TEXT,
  contracts INTEGER,
  price_cents INTEGER,
  cost_usd REAL,
  signal TEXT,
  label TEXT,
  actionable_headline TEXT,
  status TEXT NOT NULL,
  detail TEXT,
  kalshi_order_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_trades_event ON bot_trades(event_ticker, created_at);
CREATE TABLE IF NOT EXISTS bot_dedup (
  event_ticker TEXT NOT NULL,
  trigger TEXT NOT NULL,
  market_ticker TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (event_ticker, trigger, market_ticker)
);
CREATE TABLE IF NOT EXISTS bot_spent (
  event_ticker TEXT PRIMARY KEY,
  spent_usd REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""


class EthHourlyBotStore:
  def __init__(self, db_path: Path):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._init_db()

  def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(self.db_path)
    conn.row_factory = sqlite3.Row
    return conn

  def _init_db(self) -> None:
    with self._connect() as conn:
      conn.executescript(_SCHEMA)
      row = conn.execute("SELECT json FROM bot_settings WHERE id = 1").fetchone()
      if row is None:
        conn.execute(
          "INSERT INTO bot_settings (id, json) VALUES (1, ?)",
          (json.dumps(EthHourlyBotSettings().to_dict()),),
        )

  def get_settings(self) -> EthHourlyBotSettings:
    with self._connect() as conn:
      row = conn.execute("SELECT json FROM bot_settings WHERE id = 1").fetchone()
    return EthHourlyBotSettings.from_dict(json.loads(row["json"]) if row else {})

  def save_settings(self, settings: EthHourlyBotSettings) -> EthHourlyBotSettings:
    with self._connect() as conn:
      conn.execute(
        "UPDATE bot_settings SET json = ? WHERE id = 1",
        (json.dumps(settings.to_dict()),),
      )
    return settings

  def spent_usd(self, event_ticker: str) -> float:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT spent_usd FROM bot_spent WHERE event_ticker = ?",
        (event_ticker,),
      ).fetchone()
    return float(row["spent_usd"]) if row else 0.0

  def add_spent(self, event_ticker: str, amount: float) -> float:
    now = datetime.now(timezone.utc).isoformat()
    with self._connect() as conn:
      row = conn.execute(
        "SELECT spent_usd FROM bot_spent WHERE event_ticker = ?",
        (event_ticker,),
      ).fetchone()
      total = (float(row["spent_usd"]) if row else 0.0) + amount
      conn.execute(
        """
        INSERT INTO bot_spent (event_ticker, spent_usd, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(event_ticker) DO UPDATE SET spent_usd = excluded.spent_usd, updated_at = excluded.updated_at
        """,
        (event_ticker, total, now),
      )
    return total

  def already_placed(self, event_ticker: str, trigger: str, market_ticker: str) -> bool:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT 1 FROM bot_dedup WHERE event_ticker = ? AND trigger = ? AND market_ticker = ?",
        (event_ticker, trigger, market_ticker),
      ).fetchone()
    return row is not None

  def mark_placed(self, event_ticker: str, trigger: str, market_ticker: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with self._connect() as conn:
      conn.execute(
        """
        INSERT OR IGNORE INTO bot_dedup (event_ticker, trigger, market_ticker, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (event_ticker, trigger, market_ticker, now),
      )

  def log_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
    tid = trade.get("id") or str(uuid.uuid4())
    now = trade.get("created_at") or datetime.now(timezone.utc).isoformat()
    row = {
      "id": tid,
      "event_ticker": trade["event_ticker"],
      "trigger": trade["trigger"],
      "mode": trade.get("mode", "paper"),
      "market_ticker": trade.get("market_ticker"),
      "side": trade.get("side"),
      "contracts": trade.get("contracts"),
      "price_cents": trade.get("price_cents"),
      "cost_usd": trade.get("cost_usd"),
      "signal": trade.get("signal"),
      "label": trade.get("label"),
      "actionable_headline": trade.get("actionable_headline"),
      "status": trade.get("status", "filled"),
      "detail": trade.get("detail"),
      "kalshi_order_id": trade.get("kalshi_order_id"),
      "created_at": now,
    }
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO bot_trades (
          id, event_ticker, trigger, mode, market_ticker, side, contracts,
          price_cents, cost_usd, signal, label, actionable_headline,
          status, detail, kalshi_order_id, created_at
        ) VALUES (
          :id, :event_ticker, :trigger, :mode, :market_ticker, :side, :contracts,
          :price_cents, :cost_usd, :signal, :label, :actionable_headline,
          :status, :detail, :kalshi_order_id, :created_at
        )
        """,
        row,
      )
    return row

  def list_trades(self, *, limit: int = 30, event_ticker: str | None = None) -> list[dict[str, Any]]:
    with self._connect() as conn:
      if event_ticker:
        rows = conn.execute(
          """
          SELECT * FROM bot_trades WHERE event_ticker = ?
          ORDER BY created_at DESC LIMIT ?
          """,
          (event_ticker, limit),
        ).fetchall()
      else:
        rows = conn.execute(
          "SELECT * FROM bot_trades ORDER BY created_at DESC LIMIT ?",
          (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

  def status(self, event_ticker: str | None = None) -> dict[str, Any]:
    settings = self.get_settings()
    spent = self.spent_usd(event_ticker) if event_ticker else 0.0
    remaining = max(0.0, settings.max_spend_per_hour_usd - spent) if event_ticker else settings.max_spend_per_hour_usd
    return {
      "settings": settings.to_dict(),
      "event_ticker": event_ticker,
      "spent_usd": round(spent, 2),
      "remaining_usd": round(remaining, 2),
      "max_spend_per_hour_usd": settings.max_spend_per_hour_usd,
    }
