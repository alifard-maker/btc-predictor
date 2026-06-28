"""SQLite persistence for bot auto-tuning overrides (per bot store DB)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_TUNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_auto_tuning (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  json TEXT NOT NULL DEFAULT '{}'
);
"""


def migrate_auto_tuning(conn: sqlite3.Connection) -> None:
  conn.executescript(_TUNING_SCHEMA)
  row = conn.execute("SELECT json FROM bot_auto_tuning WHERE id = 1").fetchone()
  if not row:
    conn.execute(
      "INSERT INTO bot_auto_tuning (id, json) VALUES (1, ?)",
      (json.dumps({}),),
    )


def get_auto_tuning(conn: sqlite3.Connection) -> dict[str, Any]:
  migrate_auto_tuning(conn)
  row = conn.execute("SELECT json FROM bot_auto_tuning WHERE id = 1").fetchone()
  if not row:
    return {}
  try:
    return json.loads(row[0] or "{}")
  except json.JSONDecodeError:
    return {}


def save_auto_tuning(conn: sqlite3.Connection, tuning: dict[str, Any]) -> dict[str, Any]:
  migrate_auto_tuning(conn)
  payload = json.dumps(tuning)
  conn.execute("UPDATE bot_auto_tuning SET json = ? WHERE id = 1", (payload,))
  return tuning
