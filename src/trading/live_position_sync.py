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


def kalshi_sellable_contracts(kalshi: Any, market_ticker: str, side: str) -> float | None:
  """Contracts held on Kalshi for this leg; None when the API is unavailable."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return None
  net = kalshi.get_market_position(str(market_ticker))
  if net is None:
    return None
  s = str(side or "").lower()
  if s == "yes":
    return max(0.0, float(net))
  return max(0.0, -float(net))


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


def hourly_event_market_tickers_from_tab(tab: dict[str, Any]) -> set[str]:
  """Market tickers for the current hourly event (from live prediction tab)."""
  live = tab.get("live") or tab
  tickers: set[str] = set()

  def add(pick: dict[str, Any] | None) -> None:
    if pick and pick.get("ticker"):
      tickers.add(str(pick["ticker"]))

  add(live.get("primary_pick"))
  for block_key in ("strategy_threshold", "strategy_range"):
    block = live.get(block_key) or {}
    add(block.get("best_edge"))
    add(block.get("most_likely"))
    for row in block.get("contracts") or []:
      add(row)
  return tickers


def _ticker_in_hourly_event(ticker: str, event_ticker: str, allowed_tickers: set[str]) -> bool:
  t = str(ticker)
  e = str(event_ticker)
  if t in allowed_tickers:
    return True
  return t == e or t.startswith(f"{e}-")


def cancel_resting_enter_orders_for_hourly_event(
  kalshi: Any,
  event_ticker: str,
  tab: dict[str, Any],
) -> int:
  """Cancel unfilled resting BUY orders on the current hourly event only."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0
  allowed = hourly_event_market_tickers_from_tab(tab)
  cancelled = 0
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "buy":
      continue
    ticker = str(row.get("ticker") or "")
    if not ticker or not _ticker_in_hourly_event(ticker, event_ticker, allowed):
      continue
    oid = row.get("order_id")
    if not oid:
      continue
    try:
      kalshi.cancel_order(str(oid))
      cancelled += 1
      log.info("Cancelled resting enter %s on %s (event %s)", oid, ticker, event_ticker)
    except Exception as e:
      log.warning("Cancel resting enter %s on %s failed: %s", oid, ticker, e)
  if cancelled:
    log.info("Cancelled %s resting enter order(s) for hourly event %s", cancelled, event_ticker)
  return cancelled


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

  sell_count = float(contracts)
  if sellable is not None:
    sell_count = min(float(contracts), float(sellable))
  if sell_count < 0.01:
    return None

  cancel_resting_orders_for_ticker(kalshi, ticker)
  sell_cents = aggressive_exit_limit_cents(int(exit_price))
  sell_int = max(1, int(sell_count)) if sell_count >= 0.99 else 0
  if sell_int <= 0:
    return None
  exit_result = place_live_exit_sell(
    kalshi,
    market_ticker=ticker,
    side=side,
    contracts=sell_int,
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
