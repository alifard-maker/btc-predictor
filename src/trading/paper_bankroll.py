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
  paper_realized_all_time_usd REAL NOT NULL DEFAULT 0
);
"""


@dataclass
class PaperBankrollState:
  paper_bankroll_usd: float
  paper_bankroll_initial_usd: float
  paper_bankroll_started_at: str
  paper_realized_all_time_usd: float

  def to_dict(self) -> dict[str, Any]:
    initial = self.paper_bankroll_initial_usd
    since_reset = round(self.paper_bankroll_usd - initial, 2)
    pct = round(100.0 * since_reset / initial, 2) if initial > 0 else 0.0
    return {
      "paper_bankroll_usd": round(self.paper_bankroll_usd, 2),
      "paper_bankroll_initial_usd": round(initial, 2),
      "paper_bankroll_started_at": self.paper_bankroll_started_at,
      "paper_realized_all_time_usd": round(self.paper_realized_all_time_usd, 2),
      "paper_bankroll_since_reset_usd": since_reset,
      "paper_bankroll_since_reset_pct": pct,
    }


def migrate_paper_state(conn: sqlite3.Connection) -> None:
  conn.executescript(PAPER_STATE_SCHEMA)


def _row_to_state(row: sqlite3.Row) -> PaperBankrollState:
  return PaperBankrollState(
    paper_bankroll_usd=float(row["paper_bankroll_usd"]),
    paper_bankroll_initial_usd=float(row["paper_bankroll_initial_usd"]),
    paper_bankroll_started_at=str(row["paper_bankroll_started_at"]),
    paper_realized_all_time_usd=float(row["paper_realized_all_time_usd"]),
  )


def get_paper_state(conn: sqlite3.Connection) -> PaperBankrollState | None:
  row = conn.execute("SELECT * FROM bot_paper_state WHERE id = 1").fetchone()
  return _row_to_state(row) if row else None


def save_paper_state(conn: sqlite3.Connection, state: PaperBankrollState) -> None:
  conn.execute(
    """
    INSERT INTO bot_paper_state (
      id, paper_bankroll_usd, paper_bankroll_initial_usd,
      paper_bankroll_started_at, paper_realized_all_time_usd
    ) VALUES (1, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      paper_bankroll_usd = excluded.paper_bankroll_usd,
      paper_bankroll_initial_usd = excluded.paper_bankroll_initial_usd,
      paper_bankroll_started_at = excluded.paper_bankroll_started_at,
      paper_realized_all_time_usd = excluded.paper_realized_all_time_usd
    """,
    (
      state.paper_bankroll_usd,
      state.paper_bankroll_initial_usd,
      state.paper_bankroll_started_at,
      state.paper_realized_all_time_usd,
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
  )
  save_paper_state(conn, updated)
  return updated


def reset_paper_bankroll(conn: sqlite3.Connection, max_cap: float) -> PaperBankrollState:
  state = PaperBankrollState(
    paper_bankroll_usd=float(max_cap),
    paper_bankroll_initial_usd=float(max_cap),
    paper_bankroll_started_at=datetime.now(timezone.utc).isoformat(),
    paper_realized_all_time_usd=0.0,
  )
  save_paper_state(conn, state)
  return state
