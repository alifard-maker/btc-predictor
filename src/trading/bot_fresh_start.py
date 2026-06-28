"""Wipe paper bot history and restore starting bankroll."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.trading.paper_bankroll import reset_paper_bankroll


def fresh_start_paper_bot(conn: sqlite3.Connection, max_cap: float) -> dict[str, Any]:
  """Delete trades, positions, cooldowns; reset paper bankroll to max_cap."""
  conn.execute("DELETE FROM bot_trades")
  conn.execute("DELETE FROM bot_positions")
  conn.execute("DELETE FROM bot_cooldowns")
  state = reset_paper_bankroll(conn, max_cap)
  return state.to_dict()
