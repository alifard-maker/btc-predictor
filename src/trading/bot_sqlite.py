"""Shared SQLite connection settings for bot stores (WAL + busy timeout)."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_bot_db(db_path: Path | str) -> sqlite3.Connection:
  """Open a bot DB with settings that tolerate concurrent readers/writers."""
  conn = sqlite3.connect(str(db_path), timeout=30.0)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA journal_mode=WAL")
  conn.execute("PRAGMA synchronous=NORMAL")
  conn.execute("PRAGMA busy_timeout=30000")
  return conn
