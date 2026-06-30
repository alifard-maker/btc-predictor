"""Kalshi inventory checks and live exit hygiene (no naked resting sells)."""

from __future__ import annotations

import logging
from typing import Any

from src.trading.bot_position_mode import normalize_position_mode
from src.trading.live_bracket_orders import (
  aggressive_exit_limit_cents,
  cancel_resting_orders,
  cancel_resting_orders_for_ticker,
  place_live_exit_sell,
)

log = logging.getLogger(__name__)


def kalshi_sellable_contracts(kalshi: Any, market_ticker: str, side: str) -> int | None:
  """Contracts held on Kalshi for this leg; None when the API is unavailable."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return None
  net = kalshi.get_market_position(str(market_ticker))
  if net is None:
    return None
  try:
    net_i = int(net)
  except (TypeError, ValueError):
    return 0
  s = str(side or "").lower()
  if s == "yes":
    return max(0, net)
  return max(0, -net)


def resting_exit_order_id(store: Any, position_id: str) -> str | None:
  """Most recent resting live exit order for a bot position."""
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT kalshi_order_id FROM bot_trades
      WHERE position_id = ? AND action = 'exit' AND status = 'resting'
        AND kalshi_order_id IS NOT NULL AND kalshi_order_id != ''
      ORDER BY created_at DESC LIMIT 1
      """,
      (position_id,),
    ).fetchone()
  if not row or not row[0]:
    return None
  return str(row[0])


def order_still_resting(kalshi: Any, order_id: str) -> bool:
  if not kalshi or not order_id:
    return False
  for row in kalshi.list_resting_orders():
    if str(row.get("order_id") or "") == str(order_id):
      return True
  return False


def cancel_orphan_live_sell_orders(kalshi: Any, allowed_tickers: set[str]) -> int:
  """Cancel resting sells on markets where the bot has no open live leg."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0
  cancelled = 0
  allowed = {str(t) for t in allowed_tickers}
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "sell":
      continue
    ticker = str(row.get("ticker") or "")
    if not ticker or ticker in allowed:
      continue
    oid = row.get("order_id")
    if not oid:
      continue
    try:
      kalshi.cancel_order(str(oid))
      cancelled += 1
      log.info("Cancelled orphan live sell %s on %s", oid, ticker)
    except Exception as e:
      log.warning("Cancel orphan sell %s on %s failed: %s", oid, ticker, e)
  return cancelled


def live_open_tickers(store: Any, period_key: str) -> set[str]:
  tickers: set[str] = set()
  for pos in store.open_positions(period_key):
    if normalize_position_mode(pos.get("mode")) == "live":
      tickers.add(str(pos["market_ticker"]))
  return tickers


def try_live_position_exit(
  *,
  kalshi: Any,
  store: Any,
  pos: dict[str, Any],
  period_key: str,
  exit_price: int,
  contracts: int,
  entry_c: int,
  pos_mode: str,
  pick: dict[str, Any],
  exit_reason: str,
  detail_suffix: str,
  extra_detail: str,
) -> dict[str, Any] | None:
  """Place or skip a live Kalshi exit. Returns a trade row when logged."""
  cancel_resting_orders(kalshi, pos)

  pending_oid = resting_exit_order_id(store, pos["id"])
  if pending_oid and order_still_resting(kalshi, pending_oid):
    return None

  ticker = str(pos["market_ticker"])
  side = str(pos["side"])
  sellable = kalshi_sellable_contracts(kalshi, ticker, side)
  if sellable is not None and sellable <= 0:
    cancel_resting_orders_for_ticker(kalshi, ticker)
    store.close_position(pos["id"])
    detail = (
      f"Live EXIT reconciled (no Kalshi inventory for {side.upper()} on {ticker}) — "
      f"closed bot leg only · {exit_reason}: {detail_suffix}{extra_detail}"
    )
    row = store.log_trade({
      "event_ticker": period_key,
      "trigger": "continuous",
      "action": "exit",
      "mode": pos_mode,
      "market_ticker": ticker,
      "side": side,
      "contracts": contracts,
      "price_cents": exit_price,
      "entry_price_cents": entry_c,
      "exit_price_cents": exit_price,
      "cost_usd": 0,
      "pnl_usd": 0,
      "signal": pick.get("signal"),
      "label": pos.get("label"),
      "status": "reconciled",
      "detail": detail,
      "position_id": pos["id"],
    })
    log.warning("Live position reconciled (no Kalshi inventory): %s", ticker)
    return row

  sell_count = contracts
  if sellable is not None:
    sell_count = min(contracts, sellable)
  if sell_count <= 0:
    return None

  cancel_resting_orders_for_ticker(kalshi, ticker)
  sell_cents = aggressive_exit_limit_cents(int(exit_price))
  exit_result = place_live_exit_sell(
    kalshi,
    market_ticker=ticker,
    side=side,
    contracts=sell_count,
    limit_cents=sell_cents,
  )
  live_exit_oid = exit_result.get("order_id")
  fill_count = int(exit_result.get("fill_count") or 0)
  if fill_count <= 0:
    return store.log_trade({
      "event_ticker": period_key,
      "trigger": "continuous",
      "action": "exit",
      "mode": pos_mode,
      "market_ticker": ticker,
      "side": side,
      "contracts": sell_count,
      "price_cents": sell_cents,
      "entry_price_cents": entry_c,
      "exit_price_cents": sell_cents,
      "cost_usd": 0,
      "signal": pick.get("signal"),
      "label": pos.get("label"),
      "status": "resting",
      "detail": (
        f"Live EXIT order {live_exit_oid} (0 filled — resting on Kalshi; "
        f"{int(exit_result.get('remaining_count') or sell_count)} remaining) "
        f"@ {sell_cents}¢"
      ),
      "position_id": pos["id"],
      "kalshi_order_id": live_exit_oid,
    })

  return {
    "exit_result": exit_result,
    "live_exit_oid": live_exit_oid,
    "fill_count": fill_count,
    "sell_cents": sell_cents,
    "sell_count": sell_count,
  }
