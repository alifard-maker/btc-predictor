"""Persistent bot scheduler cycle metadata (survives redeploys on mounted volume)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


BOT_RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_runtime (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_cycle_at TEXT,
  last_cycle_active INTEGER NOT NULL DEFAULT 0,
  cycles_total INTEGER NOT NULL DEFAULT 0,
  stats_epoch_at TEXT
);
"""


def _ensure_stats_epoch_column(conn: sqlite3.Connection) -> None:
  cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(bot_runtime)")}
  if "stats_epoch_at" not in cols:
    conn.execute("ALTER TABLE bot_runtime ADD COLUMN stats_epoch_at TEXT")


def migrate_bot_runtime(conn: sqlite3.Connection) -> None:
  conn.executescript(BOT_RUNTIME_SCHEMA)
  _ensure_stats_epoch_column(conn)


def set_stats_epoch_at(conn: sqlite3.Connection, at_iso: str) -> str:
  """Set interval/P&L stats window start (ISO-8601 UTC). Does not delete trades."""
  migrate_bot_runtime(conn)
  row = conn.execute("SELECT id FROM bot_runtime WHERE id = 1").fetchone()
  if row is None:
    conn.execute(
      """
      INSERT INTO bot_runtime (id, last_cycle_at, last_cycle_active, cycles_total, stats_epoch_at)
      VALUES (1, NULL, 0, 0, ?)
      """,
      (at_iso,),
    )
  else:
    conn.execute("UPDATE bot_runtime SET stats_epoch_at = ? WHERE id = 1", (at_iso,))
  return at_iso


def set_stats_epoch_now(conn: sqlite3.Connection) -> str:
  """Mark interval/P&L stats as starting now (fresh start / clear history)."""
  return set_stats_epoch_at(conn, datetime.now(timezone.utc).isoformat())


def stats_epoch_at(conn: sqlite3.Connection) -> str | None:
  migrate_bot_runtime(conn)
  row = conn.execute("SELECT stats_epoch_at FROM bot_runtime WHERE id = 1").fetchone()
  if not row:
    return None
  raw = row["stats_epoch_at"]
  return str(raw) if raw else None


def record_bot_cycle(conn: sqlite3.Connection, *, active: bool) -> None:
  now = datetime.now(timezone.utc).isoformat()
  row = conn.execute("SELECT cycles_total FROM bot_runtime WHERE id = 1").fetchone()
  if row is None:
    conn.execute(
      """
      INSERT INTO bot_runtime (id, last_cycle_at, last_cycle_active, cycles_total)
      VALUES (1, ?, ?, 1)
      """,
      (now, 1 if active else 0),
    )
    return
  conn.execute(
    """
    UPDATE bot_runtime
    SET last_cycle_at = ?, last_cycle_active = ?, cycles_total = cycles_total + 1
    WHERE id = 1
    """,
    (now, 1 if active else 0),
  )


def bot_runtime_dict(conn: sqlite3.Connection) -> dict[str, Any]:
  row = conn.execute("SELECT * FROM bot_runtime WHERE id = 1").fetchone()
  if not row:
    return {
      "last_cycle_at": None,
      "last_cycle_active": False,
      "cycles_total": 0,
      "stats_epoch_at": None,
    }
  return {
    "last_cycle_at": row["last_cycle_at"],
    "last_cycle_active": bool(row["last_cycle_active"]),
    "cycles_total": int(row["cycles_total"] or 0),
    "stats_epoch_at": row["stats_epoch_at"],
  }
