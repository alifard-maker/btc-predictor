"""Mode-scoped (paper vs live) bot trade statistics."""

from __future__ import annotations

import sqlite3
from typing import Any


def _mode_clause(mode: str | None, *, prefix: str = "") -> tuple[str, list[Any]]:
  if not mode:
    return "", []
  return f" AND {prefix}mode = ?", [mode]


def interval_summary_row(
  conn: sqlite3.Connection,
  event_ticker: str,
  *,
  mode: str | None = None,
) -> dict[str, Any]:
  clause, params = _mode_clause(mode)
  row = conn.execute(
    f"""
    SELECT
      COALESCE(SUM(CASE WHEN action = 'exit' AND status = 'filled' THEN COALESCE(pnl_usd, 0) ELSE 0 END), 0) AS realized_pnl_usd,
      COALESCE(SUM(CASE WHEN action = 'enter' AND status = 'filled' THEN 1 ELSE 0 END), 0) AS enter_count,
      COALESCE(SUM(CASE WHEN action = 'exit' AND status = 'filled' THEN 1 ELSE 0 END), 0) AS exit_count,
      COALESCE(SUM(CASE WHEN action = 'enter' AND status = 'filled' THEN COALESCE(cost_usd, 0) ELSE 0 END), 0) AS total_entered_usd,
      COALESCE(SUM(CASE WHEN action = 'enter' AND status = 'resting' THEN 1 ELSE 0 END), 0) AS resting_enter_count,
      COALESCE(SUM(CASE WHEN action = 'exit' AND status = 'resting' THEN 1 ELSE 0 END), 0) AS resting_exit_count
    FROM bot_trades
    WHERE event_ticker = ?{clause}
    """,
    [event_ticker, *params],
  ).fetchone()
  if not row:
    return {
      "realized_pnl_usd": 0,
      "enter_count": 0,
      "exit_count": 0,
      "total_entered_usd": 0,
      "resting_enter_count": 0,
      "resting_exit_count": 0,
      "filled_enter_count_this_hour": 0,
    }
  out = dict(row)
  out["filled_enter_count_this_hour"] = int(out.get("enter_count") or 0)
  return out


def mode_performance_summary(
  conn: sqlite3.Connection,
  mode: str,
) -> dict[str, Any]:
  """All-time closed-trade stats for one mode (paper or live)."""
  row = conn.execute(
    """
    SELECT
      COALESCE(SUM(CASE WHEN action = 'exit' AND status = 'filled' THEN COALESCE(pnl_usd, 0) ELSE 0 END), 0) AS realized_all_time_usd,
      COALESCE(SUM(CASE WHEN action = 'enter' AND status = 'filled' THEN COALESCE(cost_usd, 0) ELSE 0 END), 0) AS total_entered_all_time_usd,
      COALESCE(SUM(CASE WHEN action = 'enter' AND status = 'filled' THEN 1 ELSE 0 END), 0) AS enter_count,
      COALESCE(SUM(CASE WHEN action = 'exit' AND status = 'filled' THEN 1 ELSE 0 END), 0) AS exit_count,
      MIN(created_at) AS first_trade_at
    FROM bot_trades
    WHERE mode = ?
    """,
    (mode,),
  ).fetchone()
  if not row:
    return {
      "mode": mode,
      "realized_all_time_usd": 0.0,
      "total_entered_all_time_usd": 0.0,
      "enter_count": 0,
      "exit_count": 0,
      "roi_vs_entered_pct": 0.0,
      "first_trade_at": None,
    }
  realized = round(float(row["realized_all_time_usd"] or 0), 2)
  entered = round(float(row["total_entered_all_time_usd"] or 0), 2)
  roi = round(100.0 * realized / entered, 1) if entered > 0 else 0.0
  return {
    "mode": mode,
    "realized_all_time_usd": realized,
    "total_entered_all_time_usd": entered,
    "enter_count": int(row["enter_count"] or 0),
    "exit_count": int(row["exit_count"] or 0),
    "roi_vs_entered_pct": roi,
    "first_trade_at": row["first_trade_at"],
  }
