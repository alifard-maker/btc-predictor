"""Per-hour extra deploy budget for live bots (interval refill)."""

from __future__ import annotations

import sqlite3
from typing import Any

LIVE_HOUR_BUDGET_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_live_hour_budget (
  event_ticker TEXT PRIMARY KEY,
  extra_budget_usd REAL NOT NULL DEFAULT 0,
  refill_count INTEGER NOT NULL DEFAULT 0
);
"""


def migrate_live_hour_budget(conn: sqlite3.Connection) -> None:
  conn.executescript(LIVE_HOUR_BUDGET_SCHEMA)


def get_live_hour_budget(conn: sqlite3.Connection, event_ticker: str) -> dict[str, Any]:
  row = conn.execute(
    "SELECT extra_budget_usd, refill_count FROM bot_live_hour_budget WHERE event_ticker = ?",
    (event_ticker,),
  ).fetchone()
  if not row:
    return {"extra_budget_usd": 0.0, "refill_count": 0}
  return {
    "extra_budget_usd": float(row["extra_budget_usd"] or 0),
    "refill_count": int(row["refill_count"] or 0),
  }


def refill_live_hour_budget(
  conn: sqlite3.Connection,
  event_ticker: str,
  max_cap: float,
) -> dict[str, Any]:
  """Add another max-cap chunk to this hour's cumulative entry allowance."""
  prev = get_live_hour_budget(conn, event_ticker)
  chunk = float(max_cap)
  extra = round(float(prev["extra_budget_usd"]) + chunk, 2)
  count = int(prev["refill_count"]) + 1
  conn.execute(
    """
    INSERT INTO bot_live_hour_budget (event_ticker, extra_budget_usd, refill_count)
    VALUES (?, ?, ?)
    ON CONFLICT(event_ticker) DO UPDATE SET
      extra_budget_usd = excluded.extra_budget_usd,
      refill_count = excluded.refill_count
    """,
    (event_ticker, extra, count),
  )
  return {"extra_budget_usd": extra, "refill_count": count, "chunk_usd": chunk}
