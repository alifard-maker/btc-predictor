"""Persist hourly auto-bet bot settings, open positions, and trade log (per asset)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class HourlyBotSettings:
  enabled: bool = False
  mode: str = "paper"  # paper | live
  max_spend_per_hour_usd: float = 25.0
  allow_strong: bool = True
  allow_actionable: bool = True
  continuous: bool = True
  reentry_cooldown_seconds: int = 120
  auto_stop_on_budget_exhausted: bool = True
  auto_stopped: bool = False

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> HourlyBotSettings:
    if not raw:
      return cls()
    return cls(
      enabled=bool(raw.get("enabled", False)),
      mode=str(raw.get("mode", "paper")),
      max_spend_per_hour_usd=float(raw.get("max_spend_per_hour_usd", 25.0)),
      allow_strong=bool(raw.get("allow_strong", True)),
      allow_actionable=bool(raw.get("allow_actionable", True)),
      continuous=bool(raw.get("continuous", True)),
      reentry_cooldown_seconds=int(raw.get("reentry_cooldown_seconds", 120)),
      auto_stop_on_budget_exhausted=bool(raw.get("auto_stop_on_budget_exhausted", True)),
      auto_stopped=bool(raw.get("auto_stopped", False)),
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
  action TEXT NOT NULL DEFAULT 'enter',
  mode TEXT NOT NULL,
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
  actionable_headline TEXT,
  status TEXT NOT NULL,
  detail TEXT,
  kalshi_order_id TEXT,
  position_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_trades_event ON bot_trades(event_ticker, created_at);
CREATE TABLE IF NOT EXISTS bot_positions (
  id TEXT PRIMARY KEY,
  event_ticker TEXT NOT NULL,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  contracts INTEGER NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  signal TEXT,
  label TEXT,
  entry_edge REAL,
  reference_price REAL,
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_bot_positions_open ON bot_positions(event_ticker, status);
CREATE TABLE IF NOT EXISTS bot_cooldowns (
  event_ticker TEXT NOT NULL,
  market_ticker TEXT NOT NULL,
  exited_at TEXT NOT NULL,
  PRIMARY KEY (event_ticker, market_ticker)
);
"""


class HourlyBotStore:
  def __init__(self, db_path: Path):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._last_period_key: str | None = None
    self._last_skip_reason: str | None = None
    self._init_db()

  def set_last_skip_reason(self, reason: str | None) -> None:
    self._last_skip_reason = reason

  def last_skip_reason(self) -> str | None:
    return self._last_skip_reason

  def sync_period(self, event_ticker: str, settings: HourlyBotSettings) -> HourlyBotSettings:
    """Clear hour-scoped auto_stop when Kalshi rolls to a new hourly event."""
    prev = self._last_period_key
    self._last_period_key = event_ticker
    if settings.auto_stopped and prev and prev != event_ticker:
      updated = HourlyBotSettings(**{**settings.to_dict(), "auto_stopped": False})
      self.save_settings(updated)
      self.set_last_skip_reason(None)
      return updated
    return settings

  def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(self.db_path)
    conn.row_factory = sqlite3.Row
    return conn

  def _migrate(self, conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_trades)").fetchall()}
    if cols and "action" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN action TEXT NOT NULL DEFAULT 'enter'")
    if cols and "pnl_usd" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN pnl_usd REAL")
    if cols and "position_id" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN position_id TEXT")
    if cols and "entry_price_cents" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN entry_price_cents INTEGER")
    if cols and "exit_price_cents" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN exit_price_cents INTEGER")

  def _init_db(self) -> None:
    with self._connect() as conn:
      conn.executescript(_SCHEMA)
      self._migrate(conn)
      row = conn.execute("SELECT json FROM bot_settings WHERE id = 1").fetchone()
      if row is None:
        conn.execute(
          "INSERT INTO bot_settings (id, json) VALUES (1, ?)",
          (json.dumps(HourlyBotSettings().to_dict()),),
        )

  def get_settings(self) -> HourlyBotSettings:
    with self._connect() as conn:
      row = conn.execute("SELECT json FROM bot_settings WHERE id = 1").fetchone()
    return HourlyBotSettings.from_dict(json.loads(row["json"]) if row else {})

  def save_settings(self, settings: HourlyBotSettings) -> HourlyBotSettings:
    with self._connect() as conn:
      conn.execute(
        "UPDATE bot_settings SET json = ? WHERE id = 1",
        (json.dumps(settings.to_dict()),),
      )
    return settings

  def open_positions(self, event_ticker: str) -> list[dict[str, Any]]:
    with self._connect() as conn:
      rows = conn.execute(
        "SELECT * FROM bot_positions WHERE event_ticker = ? AND status = 'open' ORDER BY opened_at",
        (event_ticker,),
      ).fetchall()
    return [dict(r) for r in rows]

  def open_exposure_usd(self, event_ticker: str) -> float:
    positions = self.open_positions(event_ticker)
    return round(sum(float(p.get("cost_usd") or 0) for p in positions), 2)

  def realized_pnl_usd(self, event_ticker: str) -> float:
    """Sum of filled exit P&L for this hour (wins add, losses subtract from bankroll)."""
    return self._realized_pnl_usd(event_ticker)

  def hour_bankroll_usd(self, event_ticker: str, max_hourly: float) -> float:
    """Deployable hour cap after realized P&L (floor at 0)."""
    return max(0.0, float(max_hourly) + self.realized_pnl_usd(event_ticker))

  def remaining_budget_usd(self, event_ticker: str, max_hourly: float) -> float:
    """Room for new entries: hour bankroll minus current open exposure (floor at 0)."""
    return max(0.0, self.hour_bankroll_usd(event_ticker, max_hourly) - self.open_exposure_usd(event_ticker))

  def record_exit_cooldown(
    self,
    event_ticker: str,
    market_ticker: str,
    *,
    exited_at: str | None = None,
  ) -> None:
    now = exited_at or datetime.now(timezone.utc).isoformat()
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO bot_cooldowns (event_ticker, market_ticker, exited_at)
        VALUES (?, ?, ?)
        ON CONFLICT(event_ticker, market_ticker) DO UPDATE SET exited_at = excluded.exited_at
        """,
        (event_ticker, market_ticker, now),
      )

  def is_in_cooldown(self, event_ticker: str, market_ticker: str, cooldown_seconds: int) -> bool:
    if cooldown_seconds <= 0:
      return False
    with self._connect() as conn:
      row = conn.execute(
        "SELECT exited_at FROM bot_cooldowns WHERE event_ticker = ? AND market_ticker = ?",
        (event_ticker, market_ticker),
      ).fetchone()
    if not row:
      return False
    exited_at = datetime.fromisoformat(str(row["exited_at"]).replace("Z", "+00:00"))
    if exited_at.tzinfo is None:
      exited_at = exited_at.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - exited_at).total_seconds()
    return elapsed < float(cooldown_seconds)

  def has_open_position(self, event_ticker: str, market_ticker: str) -> bool:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT 1 FROM bot_positions WHERE event_ticker = ? AND market_ticker = ? AND status = 'open'",
        (event_ticker, market_ticker),
      ).fetchone()
    return row is not None

  def open_position(self, pos: dict[str, Any]) -> dict[str, Any]:
    pid = pos.get("id") or str(uuid.uuid4())
    now = pos.get("opened_at") or datetime.now(timezone.utc).isoformat()
    row = {
      "id": pid,
      "event_ticker": pos["event_ticker"],
      "market_ticker": pos["market_ticker"],
      "side": pos["side"],
      "contracts": int(pos["contracts"]),
      "entry_price_cents": int(pos["entry_price_cents"]),
      "cost_usd": float(pos["cost_usd"]),
      "signal": pos.get("signal"),
      "label": pos.get("label"),
      "entry_edge": pos.get("entry_edge"),
      "reference_price": pos.get("reference_price"),
      "opened_at": now,
      "status": "open",
    }
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO bot_positions (
          id, event_ticker, market_ticker, side, contracts, entry_price_cents,
          cost_usd, signal, label, entry_edge, reference_price, opened_at, status
        ) VALUES (
          :id, :event_ticker, :market_ticker, :side, :contracts, :entry_price_cents,
          :cost_usd, :signal, :label, :entry_edge, :reference_price, :opened_at, :status
        )
        """,
        row,
      )
    return row

  def close_position(self, position_id: str) -> dict[str, Any] | None:
    with self._connect() as conn:
      row = conn.execute("SELECT * FROM bot_positions WHERE id = ?", (position_id,)).fetchone()
      if not row:
        return None
      conn.execute("UPDATE bot_positions SET status = 'closed' WHERE id = ?", (position_id,))
    return dict(row)

  @staticmethod
  def _exit_pnl_from_prices(row: dict[str, Any]) -> float | None:
    """Derive exit P&L from entry/exit cents when pnl_usd was not persisted."""
    entry_c = row.get("entry_price_cents")
    exit_c = row.get("exit_price_cents")
    contracts = row.get("contracts")
    side = (row.get("side") or "").lower()
    if entry_c is None or exit_c is None or contracts is None:
      return None
    entry_c, exit_c, contracts = int(entry_c), int(exit_c), int(contracts)
    if side == "yes":
      return round(contracts * (exit_c - entry_c) / 100.0, 2)
    if side == "no":
      return round(contracts * (entry_c - exit_c) / 100.0, 2)
    return None

  def _realized_pnl_usd(self, event_ticker: str) -> float:
    with self._connect() as conn:
      exits = conn.execute(
        """
        SELECT pnl_usd, entry_price_cents, exit_price_cents, contracts, side
        FROM bot_trades
        WHERE event_ticker = ? AND action = 'exit' AND status = 'filled'
        """,
        (event_ticker,),
      ).fetchall()
    total = 0.0
    for r in exits:
      row = dict(r)
      pnl = row.get("pnl_usd")
      if pnl is None:
        pnl = self._exit_pnl_from_prices(row)
      total += float(pnl or 0)
    return round(total, 2)

  def _enrich_trade(self, row: dict[str, Any]) -> dict[str, Any]:
    action = row.get("action") or "enter"
    entry_c = row.get("entry_price_cents")
    exit_c = row.get("exit_price_cents")
    price_c = row.get("price_cents")
    if entry_c is None and action == "enter" and price_c is not None:
      entry_c = price_c
    if exit_c is None and action == "exit" and price_c is not None:
      exit_c = price_c
    out = dict(row)
    out["entry_price_cents"] = entry_c
    out["exit_price_cents"] = exit_c
    if action == "exit" and out.get("pnl_usd") is not None:
      out["realized_pnl_usd"] = out["pnl_usd"]
    return out

  def hour_interval_summary(self, event_ticker: str) -> dict[str, Any]:
    """Per-hour stats. total_entered_usd sums all enter fills (can exceed max at-risk with churn)."""
    with self._connect() as conn:
      row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN action = 'exit' AND status = 'filled' THEN COALESCE(pnl_usd, 0) ELSE 0 END), 0) AS realized_pnl_usd,
          COALESCE(SUM(CASE WHEN action = 'enter' AND status = 'filled' THEN 1 ELSE 0 END), 0) AS enter_count,
          COALESCE(SUM(CASE WHEN action = 'exit' AND status = 'filled' THEN 1 ELSE 0 END), 0) AS exit_count,
          COALESCE(SUM(CASE WHEN action = 'enter' AND status = 'filled' THEN COALESCE(cost_usd, 0) ELSE 0 END), 0) AS total_entered_usd
        FROM bot_trades
        WHERE event_ticker = ?
        """,
        (event_ticker,),
      ).fetchone()
    exposure = self.open_exposure_usd(event_ticker)
    open_pos = self.open_positions(event_ticker)
    exit_count = int(row["exit_count"] or 0)
    realized = round(float(row["realized_pnl_usd"] or 0), 2)
    if exit_count > 0 and realized == 0:
      realized = self._realized_pnl_usd(event_ticker)
    return {
      "event_ticker": event_ticker,
      "realized_pnl_usd": realized,
      "enter_count": int(row["enter_count"] or 0),
      "exit_count": exit_count,
      "total_entered_usd": round(float(row["total_entered_usd"] or 0), 2),
      "open_exposure_usd": exposure,
      "open_position_count": len(open_pos),
      "net_result_usd": realized,
    }

  def log_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
    tid = trade.get("id") or str(uuid.uuid4())
    now = trade.get("created_at") or datetime.now(timezone.utc).isoformat()
    action = trade.get("action", "enter")
    entry_cents = trade.get("entry_price_cents")
    exit_cents = trade.get("exit_price_cents")
    price_cents = trade.get("price_cents")
    if action == "enter" and entry_cents is None:
      entry_cents = price_cents
    elif action == "exit":
      if exit_cents is None:
        exit_cents = price_cents
      if price_cents is None:
        price_cents = exit_cents
    row = {
      "id": tid,
      "event_ticker": trade["event_ticker"],
      "trigger": trade.get("trigger", "continuous"),
      "action": action,
      "mode": trade.get("mode", "paper"),
      "market_ticker": trade.get("market_ticker"),
      "side": trade.get("side"),
      "contracts": trade.get("contracts"),
      "price_cents": price_cents,
      "entry_price_cents": entry_cents,
      "exit_price_cents": exit_cents,
      "cost_usd": trade.get("cost_usd"),
      "pnl_usd": trade.get("pnl_usd"),
      "signal": trade.get("signal"),
      "label": trade.get("label"),
      "actionable_headline": trade.get("actionable_headline"),
      "status": trade.get("status", "filled"),
      "detail": trade.get("detail"),
      "kalshi_order_id": trade.get("kalshi_order_id"),
      "position_id": trade.get("position_id"),
      "created_at": now,
    }
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO bot_trades (
          id, event_ticker, trigger, action, mode, market_ticker, side, contracts,
          price_cents, entry_price_cents, exit_price_cents, cost_usd, pnl_usd, signal, label, actionable_headline,
          status, detail, kalshi_order_id, position_id, created_at
        ) VALUES (
          :id, :event_ticker, :trigger, :action, :mode, :market_ticker, :side, :contracts,
          :price_cents, :entry_price_cents, :exit_price_cents, :cost_usd, :pnl_usd, :signal, :label, :actionable_headline,
          :status, :detail, :kalshi_order_id, :position_id, :created_at
        )
        """,
        row,
      )
    return self._enrich_trade(row)

  def list_trades(self, *, limit: int = 30, event_ticker: str | None = None) -> list[dict[str, Any]]:
    with self._connect() as conn:
      if event_ticker:
        rows = conn.execute(
          "SELECT * FROM bot_trades WHERE event_ticker = ? ORDER BY created_at DESC LIMIT ?",
          (event_ticker, limit),
        ).fetchall()
      else:
        rows = conn.execute(
          "SELECT * FROM bot_trades ORDER BY created_at DESC LIMIT ?",
          (limit,),
        ).fetchall()
    return [self._enrich_trade(dict(r)) for r in rows]

  def last_auto_stop_trade(self) -> dict[str, Any] | None:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT * FROM bot_trades WHERE action = 'auto_stop' ORDER BY created_at DESC LIMIT 1",
      ).fetchone()
    return self._enrich_trade(dict(row)) if row else None

  def status(self, event_ticker: str | None = None) -> dict[str, Any]:
    settings = self.get_settings()
    exposure = self.open_exposure_usd(event_ticker) if event_ticker else 0.0
    bankroll = (
      self.hour_bankroll_usd(event_ticker, settings.max_spend_per_hour_usd)
      if event_ticker
      else settings.max_spend_per_hour_usd
    )
    remaining = (
      self.remaining_budget_usd(event_ticker, settings.max_spend_per_hour_usd)
      if event_ticker
      else settings.max_spend_per_hour_usd
    )
    open_pos = self.open_positions(event_ticker) if event_ticker else []
    hour_summary = self.hour_interval_summary(event_ticker) if event_ticker else None
    auto_stop_row = self.last_auto_stop_trade() if settings.auto_stopped else None
    return {
      "settings": settings.to_dict(),
      "event_ticker": event_ticker,
      "open_exposure_usd": round(exposure, 2),
      "hour_bankroll_usd": round(bankroll, 2),
      "remaining_usd": round(remaining, 2),
      "max_spend_per_hour_usd": settings.max_spend_per_hour_usd,
      "open_positions": open_pos,
      "open_position_count": len(open_pos),
      "hour_summary": hour_summary,
      "hourly_summary": hour_summary,
      "auto_stopped": settings.auto_stopped,
      "auto_stop_reason": auto_stop_row.get("detail") if auto_stop_row else None,
      "last_skip_reason": self._last_skip_reason,
    }
