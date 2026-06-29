"""Persistent paper-mode bankroll state (accumulates across hours/slots)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


PAPER_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_paper_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  paper_bankroll_usd REAL NOT NULL,
  paper_bankroll_initial_usd REAL NOT NULL,
  paper_bankroll_started_at TEXT NOT NULL,
  paper_realized_all_time_usd REAL NOT NULL DEFAULT 0,
  paper_refill_count INTEGER NOT NULL DEFAULT 0,
  paper_total_invested_usd REAL NOT NULL DEFAULT 0
);
"""


@dataclass
class PaperBankrollState:
  paper_bankroll_usd: float
  paper_bankroll_initial_usd: float
  paper_bankroll_started_at: str
  paper_realized_all_time_usd: float
  paper_refill_count: int = 0
  paper_total_invested_usd: float = 0.0

  def to_dict(self) -> dict[str, Any]:
    initial = self.paper_bankroll_initial_usd
    since_reset = round(self.paper_bankroll_usd - initial, 2)
    pct = round(100.0 * since_reset / initial, 2) if initial > 0 else 0.0
    invested = round(self.paper_total_invested_usd or initial, 2)
    net_vs_invested = round(self.paper_realized_all_time_usd, 2)
    roi_pct = round(100.0 * net_vs_invested / invested, 2) if invested > 0 else 0.0
    return {
      "paper_bankroll_usd": round(self.paper_bankroll_usd, 2),
      "paper_bankroll_initial_usd": round(initial, 2),
      "paper_bankroll_started_at": self.paper_bankroll_started_at,
      "paper_realized_all_time_usd": round(self.paper_realized_all_time_usd, 2),
      "paper_bankroll_since_reset_usd": since_reset,
      "paper_bankroll_since_reset_pct": pct,
      "paper_refill_count": int(self.paper_refill_count),
      "paper_total_invested_usd": invested,
      "paper_net_vs_invested_usd": net_vs_invested,
      "paper_roi_vs_invested_pct": roi_pct,
    }


def migrate_paper_state(conn: sqlite3.Connection) -> None:
  conn.executescript(PAPER_STATE_SCHEMA)
  cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_paper_state)").fetchall()}
  if cols and "paper_refill_count" not in cols:
    conn.execute("ALTER TABLE bot_paper_state ADD COLUMN paper_refill_count INTEGER NOT NULL DEFAULT 0")
  if cols and "paper_total_invested_usd" not in cols:
    conn.execute(
      "ALTER TABLE bot_paper_state ADD COLUMN paper_total_invested_usd REAL NOT NULL DEFAULT 0"
    )
  row = conn.execute("SELECT paper_bankroll_initial_usd FROM bot_paper_state WHERE id = 1").fetchone()
  if row is not None:
    conn.execute(
      """
      UPDATE bot_paper_state
      SET paper_total_invested_usd = CASE
        WHEN paper_total_invested_usd > 0 THEN paper_total_invested_usd
        ELSE paper_bankroll_initial_usd + (paper_refill_count * paper_bankroll_initial_usd)
      END
      WHERE id = 1
      """,
    )


def _row_to_state(row: sqlite3.Row) -> PaperBankrollState:
  keys = set(row.keys())
  invested = float(row["paper_total_invested_usd"]) if "paper_total_invested_usd" in keys else 0.0
  if invested <= 0:
    invested = float(row["paper_bankroll_initial_usd"])
  return PaperBankrollState(
    paper_bankroll_usd=float(row["paper_bankroll_usd"]),
    paper_bankroll_initial_usd=float(row["paper_bankroll_initial_usd"]),
    paper_bankroll_started_at=str(row["paper_bankroll_started_at"]),
    paper_realized_all_time_usd=float(row["paper_realized_all_time_usd"]),
    paper_refill_count=int(row["paper_refill_count"]) if "paper_refill_count" in keys else 0,
    paper_total_invested_usd=invested,
  )


def get_paper_state(conn: sqlite3.Connection) -> PaperBankrollState | None:
  row = conn.execute("SELECT * FROM bot_paper_state WHERE id = 1").fetchone()
  return _row_to_state(row) if row else None


def save_paper_state(conn: sqlite3.Connection, state: PaperBankrollState) -> None:
  invested = state.paper_total_invested_usd
  if invested <= 0:
    invested = state.paper_bankroll_initial_usd
  conn.execute(
    """
    INSERT INTO bot_paper_state (
      id, paper_bankroll_usd, paper_bankroll_initial_usd,
      paper_bankroll_started_at, paper_realized_all_time_usd,
      paper_refill_count, paper_total_invested_usd
    ) VALUES (1, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      paper_bankroll_usd = excluded.paper_bankroll_usd,
      paper_bankroll_initial_usd = excluded.paper_bankroll_initial_usd,
      paper_bankroll_started_at = excluded.paper_bankroll_started_at,
      paper_realized_all_time_usd = excluded.paper_realized_all_time_usd,
      paper_refill_count = excluded.paper_refill_count,
      paper_total_invested_usd = excluded.paper_total_invested_usd
    """,
    (
      state.paper_bankroll_usd,
      state.paper_bankroll_initial_usd,
      state.paper_bankroll_started_at,
      state.paper_realized_all_time_usd,
      state.paper_refill_count,
      invested,
    ),
  )


def ensure_paper_state(
  conn: sqlite3.Connection,
  default_cap: float,
  *,
  backfill_pnl_fn: Callable[[], float],
) -> PaperBankrollState:
  existing = get_paper_state(conn)
  if existing:
    return existing
  backfill = backfill_pnl_fn()
  initial = float(default_cap)
  bankroll = max(0.0, initial + backfill)
  state = PaperBankrollState(
    paper_bankroll_usd=bankroll,
    paper_bankroll_initial_usd=initial,
    paper_bankroll_started_at=datetime.now(timezone.utc).isoformat(),
    paper_realized_all_time_usd=round(backfill, 2),
    paper_refill_count=0,
    paper_total_invested_usd=initial,
  )
  save_paper_state(conn, state)
  return state


def apply_paper_exit_pnl(conn: sqlite3.Connection, pnl: float, default_cap: float) -> PaperBankrollState:
  state = ensure_paper_state(conn, default_cap, backfill_pnl_fn=lambda: 0.0)
  updated = PaperBankrollState(
    paper_bankroll_usd=max(0.0, state.paper_bankroll_usd + pnl),
    paper_bankroll_initial_usd=state.paper_bankroll_initial_usd,
    paper_bankroll_started_at=state.paper_bankroll_started_at,
    paper_realized_all_time_usd=round(state.paper_realized_all_time_usd + pnl, 2),
    paper_refill_count=state.paper_refill_count,
    paper_total_invested_usd=state.paper_total_invested_usd,
  )
  save_paper_state(conn, updated)
  return updated


def refill_paper_bankroll(conn: sqlite3.Connection, max_cap: float) -> PaperBankrollState:
  """Top paper bankroll back up to max_cap and track cumulative capital deployed."""
  state = ensure_paper_state(conn, max_cap, backfill_pnl_fn=lambda: 0.0)
  chunk = float(max_cap)
  invested = state.paper_total_invested_usd or state.paper_bankroll_initial_usd
  updated = PaperBankrollState(
    paper_bankroll_usd=chunk,
    paper_bankroll_initial_usd=state.paper_bankroll_initial_usd,
    paper_bankroll_started_at=state.paper_bankroll_started_at,
    paper_realized_all_time_usd=state.paper_realized_all_time_usd,
    paper_refill_count=state.paper_refill_count + 1,
    paper_total_invested_usd=round(invested + chunk, 2),
  )
  save_paper_state(conn, updated)
  return updated


def reset_paper_bankroll(conn: sqlite3.Connection, max_cap: float) -> PaperBankrollState:
  chunk = float(max_cap)
  state = PaperBankrollState(
    paper_bankroll_usd=chunk,
    paper_bankroll_initial_usd=chunk,
    paper_bankroll_started_at=datetime.now(timezone.utc).isoformat(),
    paper_realized_all_time_usd=0.0,
    paper_refill_count=0,
    paper_total_invested_usd=chunk,
  )
  save_paper_state(conn, state)
  return state


def sync_paper_cap_on_max_increase(
  conn: sqlite3.Connection,
  old_cap: float,
  new_cap: float,
) -> PaperBankrollState | None:
  """When max cap rises, never reset trades; bump bankroll only if still at old ceiling."""
  if new_cap <= old_cap:
    return None
  state = get_paper_state(conn)
  if state is None:
    return None
  if abs(state.paper_bankroll_usd - old_cap) > 0.009:
    return state
  updated = PaperBankrollState(
    paper_bankroll_usd=float(new_cap),
    paper_bankroll_initial_usd=state.paper_bankroll_initial_usd,
    paper_bankroll_started_at=state.paper_bankroll_started_at,
    paper_realized_all_time_usd=state.paper_realized_all_time_usd,
    paper_refill_count=state.paper_refill_count,
    paper_total_invested_usd=state.paper_total_invested_usd,
  )
  save_paper_state(conn, updated)
  return updated
