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
  cycles_total INTEGER NOT NULL DEFAULT 0
);
"""


def migrate_bot_runtime(conn: sqlite3.Connection) -> None:
  conn.executescript(BOT_RUNTIME_SCHEMA)


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
    }
  return {
    "last_cycle_at": row["last_cycle_at"],
    "last_cycle_active": bool(row["last_cycle_active"]),
    "cycles_total": int(row["cycles_total"] or 0),
  }
