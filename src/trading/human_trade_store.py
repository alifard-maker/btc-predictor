"""Dedicated store for dashboard manual (human) hourly trades — separate from auto-bot ledger."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class HumanTradeSettings:
  mode: str = "paper"  # paper | live
  max_stake_per_entry_usd: float = 2.50
  paper_bankroll_initial_usd: float = 100.0
  max_open_positions: int = 20

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> HumanTradeSettings:
    if not raw:
      return cls()
    return cls(
      mode=str(raw.get("mode", "paper")),
      max_stake_per_entry_usd=float(raw.get("max_stake_per_entry_usd", 2.50)),
      paper_bankroll_initial_usd=float(raw.get("paper_bankroll_initial_usd", 100.0)),
      max_open_positions=int(raw.get("max_open_positions", 20)),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS human_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_trades (
  id TEXT PRIMARY KEY,
  event_ticker TEXT NOT NULL,
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
  status TEXT NOT NULL,
  detail TEXT,
  kalshi_order_id TEXT,
  position_id TEXT,
  entry_bid_cents INTEGER,
  entry_ask_cents INTEGER,
  entry_spread_cents INTEGER,
  entry_context_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_human_trades_event ON human_trades(event_ticker, created_at);
CREATE TABLE IF NOT EXISTS human_positions (
  id TEXT PRIMARY KEY,
  event_ticker TEXT NOT NULL,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  contracts INTEGER NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  signal TEXT,
  label TEXT,
  contract_type TEXT,
  strike_type TEXT,
  floor_strike REAL,
  cap_strike REAL,
  mode TEXT NOT NULL DEFAULT 'paper',
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_human_positions_open ON human_positions(event_ticker, status);
"""


class HumanTradeStore:
  def __init__(self, db_path: Path):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._init_db()

  def _connect(self) -> sqlite3.Connection:
    from src.trading.bot_sqlite import connect_bot_db

    return connect_bot_db(self.db_path)

  def _migrate(self, conn: sqlite3.Connection) -> None:
    from src.trading.paper_bankroll import migrate_paper_state

    migrate_paper_state(conn)

  def _init_db(self) -> None:
    with self._connect() as conn:
      conn.executescript(_SCHEMA)
      self._migrate(conn)
      row = conn.execute("SELECT json FROM human_settings WHERE id = 1").fetchone()
      if not row:
        conn.execute(
          "INSERT INTO human_settings (id, json) VALUES (1, ?)",
          (json.dumps(HumanTradeSettings().to_dict()),),
        )

  def get_settings(self) -> HumanTradeSettings:
    with self._connect() as conn:
      row = conn.execute("SELECT json FROM human_settings WHERE id = 1").fetchone()
    raw = json.loads(row["json"]) if row else {}
    return HumanTradeSettings.from_dict(raw)

  def save_settings(self, settings: HumanTradeSettings) -> None:
    with self._connect() as conn:
      conn.execute(
        "UPDATE human_settings SET json = ? WHERE id = 1",
        (json.dumps(settings.to_dict()),),
      )

  def open_positions(self, event_ticker: str | None = None) -> list[dict[str, Any]]:
    with self._connect() as conn:
      if event_ticker:
        rows = conn.execute(
          """
          SELECT * FROM human_positions
          WHERE status = 'open' AND event_ticker = ?
          ORDER BY opened_at
          """,
          (event_ticker,),
        ).fetchall()
      else:
        rows = conn.execute(
          "SELECT * FROM human_positions WHERE status = 'open' ORDER BY opened_at",
        ).fetchall()
    return [dict(r) for r in rows]

  def open_position(self, pos: dict[str, Any]) -> dict[str, Any]:
    from src.trading.hourly_event_time import canonical_hourly_event_ticker

    pid = pos.get("id") or str(uuid.uuid4())
    now = pos.get("opened_at") or datetime.now(timezone.utc).isoformat()
    row = {
      "id": pid,
      "event_ticker": canonical_hourly_event_ticker(str(pos["event_ticker"])),
      "market_ticker": pos["market_ticker"],
      "side": pos["side"],
      "contracts": int(pos["contracts"]),
      "entry_price_cents": int(pos["entry_price_cents"]),
      "cost_usd": float(pos["cost_usd"]),
      "signal": pos.get("signal"),
      "label": pos.get("label"),
      "contract_type": pos.get("contract_type"),
      "strike_type": pos.get("strike_type"),
      "floor_strike": pos.get("floor_strike"),
      "cap_strike": pos.get("cap_strike"),
      "mode": pos.get("mode") or "paper",
      "opened_at": now,
      "status": "open",
    }
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO human_positions (
          id, event_ticker, market_ticker, side, contracts, entry_price_cents,
          cost_usd, signal, label, contract_type, strike_type, floor_strike,
          cap_strike, mode, opened_at, status
        ) VALUES (
          :id, :event_ticker, :market_ticker, :side, :contracts, :entry_price_cents,
          :cost_usd, :signal, :label, :contract_type, :strike_type, :floor_strike,
          :cap_strike, :mode, :opened_at, :status
        )
        """,
        row,
      )
    return row

  def close_position(self, position_id: str) -> dict[str, Any] | None:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT * FROM human_positions WHERE id = ?",
        (position_id,),
      ).fetchone()
      if not row:
        return None
      conn.execute(
        "UPDATE human_positions SET status = 'closed' WHERE id = ?",
        (position_id,),
      )
    return dict(row)

  def purge_mode_trades_for_event(self, event_ticker: str, *, mode: str = "live") -> int:
    """Delete enter/exit rows for one event+mode (used when rebuilding from Kalshi)."""
    from src.trading.hourly_event_time import canonical_hourly_event_ticker

    event = canonical_hourly_event_ticker(str(event_ticker))
    mode_l = str(mode).lower()
    with self._connect() as conn:
      cur = conn.execute(
        """
        DELETE FROM human_trades
        WHERE event_ticker = ?
          AND lower(mode) = ?
        """,
        (event, mode_l),
      )
      return int(cur.rowcount or 0)

  def update_trade_exit(
    self,
    trade_id: str,
    *,
    exit_price_cents: int,
    pnl_usd: float,
    detail: str | None = None,
  ) -> dict[str, Any] | None:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT * FROM human_trades WHERE id = ?",
        (trade_id,),
      ).fetchone()
      if not row:
        return None
      conn.execute(
        """
        UPDATE human_trades
        SET exit_price_cents = ?,
            price_cents = ?,
            pnl_usd = ?,
            detail = COALESCE(?, detail)
        WHERE id = ?
        """,
        (int(exit_price_cents), int(exit_price_cents), float(pnl_usd), detail, trade_id),
      )
      updated = conn.execute(
        "SELECT * FROM human_trades WHERE id = ?",
        (trade_id,),
      ).fetchone()
    return dict(updated) if updated else None

  def find_open_position(
    self,
    *,
    event_ticker: str,
    market_ticker: str,
    side: str,
  ) -> dict[str, Any] | None:
    with self._connect() as conn:
      row = conn.execute(
        """
        SELECT * FROM human_positions
        WHERE status = 'open'
          AND event_ticker = ?
          AND market_ticker = ?
          AND side = ?
        ORDER BY opened_at DESC
        LIMIT 1
        """,
        (event_ticker, market_ticker, side),
      ).fetchone()
    return dict(row) if row else None

  def log_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
    from src.trading.hourly_event_time import canonical_hourly_event_ticker

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
    ctx = trade.get("entry_context")
    row = {
      "id": tid,
      "event_ticker": canonical_hourly_event_ticker(str(trade["event_ticker"])),
      "action": action,
      "mode": trade.get("mode") or "paper",
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
      "status": trade.get("status", "filled"),
      "detail": trade.get("detail"),
      "kalshi_order_id": trade.get("kalshi_order_id"),
      "position_id": trade.get("position_id"),
      "entry_bid_cents": trade.get("entry_bid_cents"),
      "entry_ask_cents": trade.get("entry_ask_cents"),
      "entry_spread_cents": trade.get("entry_spread_cents"),
      "entry_context_json": json.dumps(ctx) if ctx is not None else None,
      "created_at": now,
    }
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO human_trades (
          id, event_ticker, action, mode, market_ticker, side, contracts,
          price_cents, entry_price_cents, exit_price_cents, cost_usd, pnl_usd,
          signal, label, status, detail, kalshi_order_id, position_id,
          entry_bid_cents, entry_ask_cents, entry_spread_cents,
          entry_context_json, created_at
        ) VALUES (
          :id, :event_ticker, :action, :mode, :market_ticker, :side, :contracts,
          :price_cents, :entry_price_cents, :exit_price_cents, :cost_usd, :pnl_usd,
          :signal, :label, :status, :detail, :kalshi_order_id, :position_id,
          :entry_bid_cents, :entry_ask_cents, :entry_spread_cents,
          :entry_context_json, :created_at
        )
        """,
        row,
      )
    out = dict(row)
    if out.get("entry_context_json"):
      try:
        out["entry_context"] = json.loads(out["entry_context_json"])
      except json.JSONDecodeError:
        pass
    return out

  def list_paper_exits(self, *, limit: int = 200) -> list[dict[str, Any]]:
    """Paper-only exit rows (legacy). Prefer list_exit_log for the dashboard."""
    return self.list_exit_log(limit=limit, modes=("paper",))

  def list_exit_log(
    self,
    *,
    limit: int = 200,
    modes: tuple[str, ...] = ("paper", "live"),
  ) -> list[dict[str, Any]]:
    """Closed exit rows for the manual P&L log (paper and/or live)."""
    mode_list = [str(m).lower() for m in modes if m]
    if not mode_list:
      mode_list = ["paper", "live"]
    placeholders = ",".join("?" for _ in mode_list)
    with self._connect() as conn:
      rows = conn.execute(
        f"""
        SELECT * FROM human_trades
        WHERE action = 'exit'
          AND lower(mode) IN ({placeholders})
          AND status IN ('filled', 'reconciled')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (*mode_list, int(limit)),
      ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
      row = dict(r)
      raw = row.pop("entry_context_json", None)
      if raw:
        try:
          row["entry_context"] = json.loads(raw)
        except json.JSONDecodeError:
          pass
      cost = row.get("cost_usd")
      pnl = row.get("pnl_usd")
      try:
        cost_f = float(cost) if cost is not None else None
        pnl_f = float(pnl) if pnl is not None else None
      except (TypeError, ValueError):
        cost_f = pnl_f = None
      if cost_f is not None and cost_f > 0.009 and pnl_f is not None:
        row["return_pct"] = round(pnl_f / cost_f * 100.0, 1)
      else:
        row["return_pct"] = None
      out.append(row)
    return out

  def list_trades(
    self,
    *,
    limit: int = 100,
    event_ticker: str | None = None,
    since: str | None = None,
  ) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if event_ticker:
      clauses.append("event_ticker = ?")
      params.append(event_ticker)
    if since:
      clauses.append("created_at >= ?")
      params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with self._connect() as conn:
      rows = conn.execute(
        f"""
        SELECT * FROM human_trades
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
      ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
      row = dict(r)
      raw = row.pop("entry_context_json", None)
      if raw:
        try:
          row["entry_context"] = json.loads(raw)
        except json.JSONDecodeError:
          pass
      out.append(row)
    return out

  def sum_realized_pnl_usd(self, *, mode: str = "paper") -> float:
    """Sum filled exit P&L for a mode (source of truth for paper ledger)."""
    mode_l = str(mode).lower()
    with self._connect() as conn:
      row = conn.execute(
        """
        SELECT COALESCE(SUM(pnl_usd), 0) AS total
        FROM human_trades
        WHERE action = 'exit'
          AND lower(mode) = ?
          AND status IN ('filled', 'reconciled')
          AND pnl_usd IS NOT NULL
        """,
        (mode_l,),
      ).fetchone()
    return round(float(row["total"] if row else 0.0), 2)

  def pnl_summary(self, *, mode: str = "paper") -> dict[str, Any]:
    """Closed-leg P&L summary for paper (or live) manual exits."""
    mode_l = str(mode).lower()
    with self._connect() as conn:
      rows = conn.execute(
        """
        SELECT pnl_usd, created_at
        FROM human_trades
        WHERE action = 'exit'
          AND lower(mode) = ?
          AND status IN ('filled', 'reconciled')
          AND pnl_usd IS NOT NULL
        ORDER BY created_at DESC
        """,
        (mode_l,),
      ).fetchall()
    pnls = [float(r["pnl_usd"]) for r in rows]
    total = round(sum(pnls), 2)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    pushes = sum(1 for p in pnls if p == 0)
    decisive = wins + losses
    today_prefix = datetime.now(timezone.utc).date().isoformat()
    today_pnls = [
      float(r["pnl_usd"])
      for r in rows
      if str(r["created_at"] or "").startswith(today_prefix)
    ]
    return {
      "mode": mode_l,
      "closed_legs": len(pnls),
      "realized_pnl_usd": total,
      "today_realized_pnl_usd": round(sum(today_pnls), 2),
      "today_closed_legs": len(today_pnls),
      "wins": wins,
      "losses": losses,
      "pushes": pushes,
      # Exclude scratch/+0 settles (cash-back) from WR denominator.
      "win_rate": round(wins / decisive, 3) if decisive else None,
      "avg_pnl_usd": round(total / len(pnls), 2) if pnls else None,
    }

  def get_paper_state_dict(self, default_cap: float) -> dict[str, Any]:
    from src.trading.paper_bankroll import ensure_paper_state

    with self._connect() as conn:
      return ensure_paper_state(
        conn,
        default_cap,
        backfill_pnl_fn=lambda: self.sum_realized_pnl_usd(mode="paper"),
      ).to_dict()

  def debit_paper_for_entry(self, cost_usd: float, default_cap: float) -> bool:
    from src.trading.paper_bankroll import PaperBankrollState, ensure_paper_state, save_paper_state

    with self._connect() as conn:
      state = ensure_paper_state(
        conn,
        default_cap,
        backfill_pnl_fn=lambda: self.sum_realized_pnl_usd(mode="paper"),
      )
      if state.paper_bankroll_usd < float(cost_usd) - 0.001:
        return False
      updated = PaperBankrollState(
        paper_bankroll_usd=round(state.paper_bankroll_usd - float(cost_usd), 2),
        paper_bankroll_initial_usd=state.paper_bankroll_initial_usd,
        paper_bankroll_started_at=state.paper_bankroll_started_at,
        paper_realized_all_time_usd=state.paper_realized_all_time_usd,
        paper_refill_count=state.paper_refill_count,
        paper_total_invested_usd=state.paper_total_invested_usd,
      )
      save_paper_state(conn, updated)
    return True

  def open_paper_cost_usd(self) -> float:
    with self._connect() as conn:
      row = conn.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0) AS total
        FROM human_positions
        WHERE status = 'open' AND lower(mode) = 'paper'
        """,
      ).fetchone()
    return round(float(row["total"] if row else 0.0), 2)

  def apply_paper_exit_settlement(
    self,
    cost_usd: float,
    pnl: float,
    default_cap: float,
  ) -> dict[str, Any]:
    """Return entry capital + P&L to paper bankroll (enter debits cost; exit must restore it)."""
    from src.trading.paper_bankroll import PaperBankrollState, ensure_paper_state, save_paper_state

    proceeds = round(float(cost_usd) + float(pnl), 2)
    with self._connect() as conn:
      state = ensure_paper_state(
        conn,
        default_cap,
        backfill_pnl_fn=lambda: self.sum_realized_pnl_usd(mode="paper"),
      )
      updated = PaperBankrollState(
        paper_bankroll_usd=round(max(0.0, state.paper_bankroll_usd + proceeds), 2),
        paper_bankroll_initial_usd=state.paper_bankroll_initial_usd,
        paper_bankroll_started_at=state.paper_bankroll_started_at,
        paper_realized_all_time_usd=round(state.paper_realized_all_time_usd + float(pnl), 2),
        paper_refill_count=state.paper_refill_count,
        paper_total_invested_usd=state.paper_total_invested_usd,
      )
      save_paper_state(conn, updated)
      return updated.to_dict()

  def reconcile_paper_bankroll(self, default_cap: float) -> dict[str, Any]:
    """Heal bankroll = initial + realized exits − open paper costs (fixes principal-not-returned bug)."""
    from src.trading.paper_bankroll import PaperBankrollState, ensure_paper_state, save_paper_state

    realized = self.sum_realized_pnl_usd(mode="paper")
    open_cost = self.open_paper_cost_usd()
    with self._connect() as conn:
      state = ensure_paper_state(
        conn,
        default_cap,
        backfill_pnl_fn=lambda: realized,
      )
      expected = round(float(state.paper_bankroll_initial_usd) + realized - open_cost, 2)
      expected = max(0.0, expected)
      if (
        abs(state.paper_bankroll_usd - expected) > 0.009
        or abs(state.paper_realized_all_time_usd - realized) > 0.009
      ):
        updated = PaperBankrollState(
          paper_bankroll_usd=expected,
          paper_bankroll_initial_usd=state.paper_bankroll_initial_usd,
          paper_bankroll_started_at=state.paper_bankroll_started_at,
          paper_realized_all_time_usd=realized,
          paper_refill_count=state.paper_refill_count,
          paper_total_invested_usd=state.paper_total_invested_usd,
        )
        save_paper_state(conn, updated)
        out = updated.to_dict()
        out["reconciled"] = True
        return out
      out = state.to_dict()
      out["reconciled"] = False
      return out

  def status(self, event_ticker: str | None = None) -> dict[str, Any]:
    settings = self.get_settings()
    paper = self.reconcile_paper_bankroll(settings.paper_bankroll_initial_usd)
    open_all = self.open_positions()
    # Manual UI must see every open leg (any hour) — filtering by current event
    # hid locked capital and made "Buys paused" look like lost positions.
    open_pos = list(open_all)
    hour_trades = (
      self.list_trades(limit=50, event_ticker=event_ticker)
      if event_ticker
      else []
    )
    recent = self.list_trades(limit=80)
    paper_pnl = self.pnl_summary(mode="paper")
    live_pnl = self.pnl_summary(mode="live")
    open_cost = self.open_paper_cost_usd()
    initial = float(settings.paper_bankroll_initial_usd)
    realized = float(paper_pnl.get("realized_pnl_usd") or 0.0)
    expected = round(max(0.0, initial + realized - open_cost), 2)
    paper = dict(paper)
    paper["paper_open_cost_usd"] = open_cost
    paper["paper_expected_bankroll_usd"] = expected
    paper["paper_locked_legs"] = len(open_all)
    # Force heal if ledger drifted (should be rare after reconcile).
    if abs(float(paper.get("paper_bankroll_usd") or 0) - expected) > 0.009:
      paper = self.reconcile_paper_bankroll(settings.paper_bankroll_initial_usd)
      paper = dict(paper)
      paper["paper_open_cost_usd"] = open_cost
      paper["paper_expected_bankroll_usd"] = expected
      paper["paper_locked_legs"] = len(open_all)
    exit_log = self.list_exit_log(limit=500)
    return {
      "settings": settings.to_dict(),
      "paper_bankroll": paper,
      "paper_pnl": paper_pnl,
      "live_pnl": live_pnl,
      "open_positions": open_pos,
      "open_position_count": len(open_pos),
      "all_open_position_count": len(open_all),
      "hour_trades": hour_trades,
      "recent_trades": recent,
      "paper_recent_trades": [
        t for t in recent if str(t.get("mode") or "").lower() == "paper"
      ][:40],
      "paper_exit_log": [t for t in exit_log if str(t.get("mode") or "").lower() == "paper"],
      "exit_log": exit_log,
      "event_ticker": event_ticker,
    }
