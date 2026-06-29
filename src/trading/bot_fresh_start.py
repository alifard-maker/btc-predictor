"""Wipe paper bot history and restore starting bankroll."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.trading.paper_bankroll import reset_paper_bankroll


def fresh_start_paper_bot(conn: sqlite3.Connection, max_cap: float) -> dict[str, Any]:
  """Delete trades, positions, cooldowns, tuning; reset paper bankroll to max_cap."""
  from src.trading.bot_runtime import migrate_bot_runtime
  from src.trading.bot_tuning_store import save_auto_tuning

  conn.execute("DELETE FROM bot_trades")
  conn.execute("DELETE FROM bot_positions")
  conn.execute("DELETE FROM bot_cooldowns")
  from src.trading.bot_cheap_leg_cooldown import clear_cheap_leg_cut_cooldowns

  clear_cheap_leg_cut_cooldowns(conn)
  save_auto_tuning(conn, {})
  migrate_bot_runtime(conn)
  conn.execute(
    """
    UPDATE bot_runtime
    SET last_cycle_at = NULL, last_cycle_active = 0, cycles_total = 0
    WHERE id = 1
    """,
  )
  state = reset_paper_bankroll(conn, max_cap)
  return state.to_dict()
