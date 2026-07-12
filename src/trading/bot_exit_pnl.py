"""Derive effective exit P&L from bot_trades rows."""

from __future__ import annotations

from typing import Any

_PNL_EPSILON = 0.005


def effective_exit_pnl_usd(row: dict[str, Any]) -> float:
  """
  Return realized exit P&L for one trade row.

  When pnl_usd was stored as 0 but entry/exit prices differ, recompute from
  prices so scratch rows with wrong bookkeeping still count in summaries.
  """
  from src.trading.paper_execution import leg_pnl_usd

  entry_c = row.get("entry_price_cents")
  exit_c = row.get("exit_price_cents")
  if exit_c is None and row.get("action") == "exit":
    exit_c = row.get("price_cents")
  contracts = row.get("contracts")
  logged = row.get("pnl_usd")

  computed: float | None = None
  if entry_c is not None and exit_c is not None and contracts is not None:
    if int(entry_c) != int(exit_c):
      computed = float(
        leg_pnl_usd(
          entry_price_cents=int(entry_c),
          mark_or_exit_cents=int(exit_c),
          contracts=int(contracts),
        )
        or 0.0,
      )

  if computed is not None:
    if logged is None:
      return round(computed, 2)
    logged_f = float(logged)
    if abs(logged_f) < _PNL_EPSILON and abs(computed) >= _PNL_EPSILON:
      return round(computed, 2)

  if logged is not None:
    return round(float(logged), 2)
  if computed is not None:
    return round(computed, 2)
  return 0.0
