"""Force-close open bot positions when an hour/slot period rolls over."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.trading.paper_execution import leg_pnl_usd, paper_exit_fill

log = logging.getLogger(__name__)


def exit_pnl_usd(
  *,
  side: str,
  contracts: int,
  entry_cents: int,
  exit_cents: int,
) -> float:
  _ = side
  return float(
    leg_pnl_usd(
      entry_price_cents=entry_cents,
      mark_or_exit_cents=exit_cents,
      contracts=contracts,
    )
    or 0.0,
  )


def resolve_rollover_exit_cents(
  pos: dict[str, Any],
  *,
  current_market_ticker: str | None,
  quote: dict[str, Any] | None,
  yes_mid_cents: int | None,
  price_for_side: Callable[[int | None, str], int | None],
) -> int:
  last_mark = pos.get("last_mark_cents")
  if last_mark is not None:
    return int(last_mark)
  if current_market_ticker and pos.get("market_ticker") == current_market_ticker:
    if quote:
      fill = paper_exit_fill(pick=quote, side=str(pos.get("side") or ""))
      if fill.get("ok") and fill.get("price_cents") is not None:
        return int(fill["price_cents"])
    mark = price_for_side(yes_mid_cents, str(pos.get("side") or ""))
    if mark is not None:
      return mark
  return int(pos["entry_price_cents"])


def force_close_period_positions(
  store: Any,
  prev_period_key: str,
  *,
  exit_cents_for_position: Callable[[dict[str, Any]], int],
  settings: Any,
  log_label: str,
) -> list[dict[str, Any]]:
  """Close any open legs still tagged to the previous hour/slot."""
  results: list[dict[str, Any]] = []
  for pos in store.open_positions(prev_period_key):
    entry_c = int(pos["entry_price_cents"])
    contracts = int(pos["contracts"])
    exit_price = exit_cents_for_position(pos)
    pnl = exit_pnl_usd(
      side=str(pos["side"]),
      contracts=contracts,
      entry_cents=entry_c,
      exit_cents=exit_price,
    )
    store.close_position(pos["id"])
    detail = (
      f"Paper EXIT (PERIOD ROLLOVER): {pos['side'].upper()} ×{contracts} "
      f"@ {exit_price}¢ (entry {entry_c}¢) — forced close at {log_label} end"
    )
    row = store.log_trade({
      "event_ticker": prev_period_key,
      "trigger": "period_rollover",
      "action": "exit",
      "mode": settings.mode,
      "market_ticker": pos.get("market_ticker"),
      "side": pos["side"],
      "contracts": contracts,
      "price_cents": exit_price,
      "entry_price_cents": entry_c,
      "exit_price_cents": exit_price,
      "cost_usd": 0,
      "pnl_usd": pnl,
      "signal": pos.get("signal"),
      "label": pos.get("label"),
      "status": "filled",
      "detail": detail,
      "position_id": pos["id"],
    })
    log.info("%s bot period rollover exit: %s", log_label, detail)
    results.append(row)
  return results
