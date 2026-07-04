"""Aggregate bot win/loss counts per hour or 15m slot (event_ticker)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

_PNL_EPSILON = 0.005


def _classify_interval(realized_pnl: float, *, exit_count: int, enter_count: int) -> str | None:
  if exit_count <= 0:
    return "pending" if enter_count > 0 else None
  if realized_pnl > _PNL_EPSILON:
    return "profit"
  if realized_pnl < -_PNL_EPSILON:
    return "loss"
  return "breakeven"


def compute_interval_performance(
  conn: sqlite3.Connection,
  *,
  current_event_ticker: str | None,
  realized_pnl_fn: Callable[[str], float] | None = None,
  mode: str | None = None,
) -> dict[str, Any]:
  """
  Count completed intervals (hours/slots) as profit or loss from closed exit P&L.

  The active interval is reported separately and excluded from all-time W/L counts
  until it rolls over.
  """
  mode_clause = ""
  mode_params: list[Any] = []
  if mode:
    mode_clause = " AND mode = ?"
    mode_params.append(mode)

  from src.trading.bot_runtime import event_in_stats_epoch, stats_epoch_at

  epoch = stats_epoch_at(conn)

  rows = conn.execute(
    f"""
    SELECT
      event_ticker,
      COALESCE(SUM(CASE WHEN action = 'exit' AND status IN ('filled', 'reconciled') THEN COALESCE(pnl_usd, 0) ELSE 0 END), 0) AS realized_pnl_usd,
      COALESCE(SUM(CASE WHEN action = 'exit' AND status IN ('filled', 'reconciled') THEN 1 ELSE 0 END), 0) AS exit_count,
      COALESCE(SUM(CASE WHEN action = 'enter' AND status = 'filled' THEN 1 ELSE 0 END), 0) AS enter_count,
      MIN(created_at) AS first_trade_at
    FROM bot_trades
    WHERE action NOT IN ('auto_stop', 'paper_refill', 'live_hour_refill'){mode_clause}
    GROUP BY event_ticker
    ORDER BY first_trade_at ASC
    """,
    mode_params,
  ).fetchall()

  profit_count = 0
  loss_count = 0
  breakeven_count = 0
  pending_past_count = 0
  net_pnl_usd = 0.0
  profit_pnl_usd = 0.0
  loss_pnl_usd = 0.0
  current_interval: dict[str, Any] | None = None

  for row in rows:
    event = str(row["event_ticker"])
    if not event_in_stats_epoch(
      event,
      epoch,
      first_trade_at=row["first_trade_at"],
    ):
      continue
    realized = round(float(row["realized_pnl_usd"] or 0), 2)
    exit_count = int(row["exit_count"] or 0)
    enter_count = int(row["enter_count"] or 0)
    if exit_count > 0 and realized_pnl_fn is not None:
      realized = round(float(realized_pnl_fn(event)), 2)

    outcome = _classify_interval(realized, exit_count=exit_count, enter_count=enter_count)
    if event == current_event_ticker:
      current_interval = {
        "event_ticker": event,
        "realized_pnl_usd": realized,
        "exit_count": exit_count,
        "enter_count": enter_count,
        "outcome": outcome or "idle",
      }
      continue

    if outcome == "profit":
      profit_count += 1
      profit_pnl_usd += realized
      net_pnl_usd += realized
    elif outcome == "loss":
      loss_count += 1
      loss_pnl_usd += realized
      net_pnl_usd += realized
    elif outcome == "breakeven":
      breakeven_count += 1
    elif outcome == "pending":
      pending_past_count += 1

  scored = profit_count + loss_count + breakeven_count
  win_rate_pct = round(100.0 * profit_count / scored, 1) if scored else None

  return {
    "mode": mode,
    "profit_intervals": profit_count,
    "loss_intervals": loss_count,
    "breakeven_intervals": breakeven_count,
    "intervals_scored": scored,
    "intervals_pending": pending_past_count,
    "win_rate_pct": win_rate_pct,
    "net_interval_pnl_usd": round(net_pnl_usd, 2),
    "interval_profit_usd": round(profit_pnl_usd, 2),
    "interval_loss_usd": round(loss_pnl_usd, 2),
    "current_interval": current_interval,
    "stats_epoch_at": epoch,
  }
