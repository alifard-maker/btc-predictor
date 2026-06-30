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


def sync_live_positions_from_kalshi(
  store: Any,
  kalshi: Any,
  event_ticker: str,
) -> dict[str, Any]:
  """Align open live bot legs with Kalshi inventory (contracts + merge duplicates)."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": True, "changes": []}

  groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
  for pos in store.open_positions(event_ticker):
    if normalize_position_mode(pos.get("mode")) != "live":
      continue
    key = (str(pos["market_ticker"]), str(pos.get("side") or "").lower())
    groups.setdefault(key, []).append(pos)

  changes: list[dict[str, Any]] = []
  for (ticker, side), legs in groups.items():
    sellable = kalshi_sellable_contracts(kalshi, ticker, side)
    if sellable is None:
      continue
    bot_total = sum(int(p.get("contracts") or 0) for p in legs)
    target = int(round(float(sellable)))
    if abs(float(sellable) - float(bot_total)) < 0.05:
      continue

    if target <= 0:
      log.warning(
        "Kalshi inventory 0 for %s %s but bot tracks %s contracts — keeping bot legs open",
        ticker,
        side,
        bot_total,
      )
      changes.append({
        "ticker": ticker,
        "side": side,
        "action": "inventory_zero_kept_open",
        "bot_contracts": bot_total,
      })
      continue

    legs.sort(key=lambda p: str(p.get("opened_at") or ""))
    primary = legs[0]
    entry_c = int(primary.get("entry_price_cents") or 0)
    new_cost = round(target * entry_c / 100.0, 2) if entry_c else float(primary.get("cost_usd") or 0)
    store.update_position_contracts(
      str(primary["id"]),
      contracts=target,
      cost_usd=new_cost,
    )
    changes.append({
      "ticker": ticker,
      "side": side,
      "action": "synced",
      "from_contracts": bot_total,
      "to_contracts": target,
      "position_id": str(primary["id"]),
    })
    for extra in legs[1:]:
      store.close_position(str(extra["id"]))
      changes.append({
        "ticker": ticker,
        "side": side,
        "action": "merged_duplicate",
        "position_id": str(extra["id"]),
      })

  return {"ok": True, "changes": changes}


def run_live_position_hygiene(
  *,
  store: Any,
  kalshi: Any,
  event_ticker: str,
  tab: dict[str, Any],
  settings_enabled: bool,
) -> dict[str, Any]:
  """Sync inventory, cancel orphans, and optionally cancel resting enters when auto-bet is off."""
  sync = sync_live_positions_from_kalshi(store, kalshi, event_ticker)
  orphans = cancel_orphan_live_sell_orders(
    kalshi, live_open_tickers(store, event_ticker),
  )
  resting_cancelled = 0
  if not settings_enabled:
    resting_cancelled = cancel_resting_enter_orders_for_hourly_event(
      kalshi, event_ticker, tab,
    )
  return {
    **sync,
    "orphan_sells_cancelled": orphans,
    "resting_enters_cancelled": resting_cancelled,
  }


def cancel_resting_enter_orders_for_market_tickers(
  kalshi: Any,
  market_tickers: set[str],
) -> int:
  """Cancel unfilled resting BUY orders on specific market tickers only."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0
  allowed = {str(t) for t in market_tickers if t}
  if not allowed:
    return 0
  cancelled = 0
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "buy":
      continue
    ticker = str(row.get("ticker") or "")
    if ticker not in allowed:
      continue
    oid = row.get("order_id")
    if not oid:
      continue
    try:
      kalshi.cancel_order(str(oid))
      cancelled += 1
      log.info("Cancelled resting enter %s on %s", oid, ticker)
    except Exception as e:
      log.warning("Cancel resting enter %s on %s failed: %s", oid, ticker, e)
  return cancelled


def run_live_slot_hygiene(
  *,
  store: Any,
  kalshi: Any,
  period_key: str,
  market_ticker: str | None,
  settings_enabled: bool,
) -> dict[str, Any]:
  """Sync inventory and cancel orphans for a 15m slot (single market)."""
  sync = sync_live_positions_from_kalshi(store, kalshi, period_key)
  orphans = cancel_orphan_live_sell_orders(
    kalshi, live_open_tickers(store, period_key),
  )
  resting_cancelled = 0
  if not settings_enabled and market_ticker:
    resting_cancelled = cancel_resting_enter_orders_for_market_tickers(
      kalshi, {str(market_ticker)},
    )
  return {
    **sync,
    "orphan_sells_cancelled": orphans,
    "resting_enters_cancelled": resting_cancelled,
  }


def verify_kalshi_exit_fill(
  *,
  sellable_before: float | None,
  sellable_after: float | None,
  claimed_fill: int,
) -> int:
  """Confirmed contracts sold on Kalshi; never trust API fill_count alone."""
  if sellable_before is not None and sellable_after is not None:
    sold = max(0.0, float(sellable_before) - float(sellable_after))
    if sold < 0.05:
      return 0
    return min(int(round(sold)), max(0, int(claimed_fill)))
  return 0


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
    log.warning(
      "Live exit skipped — no Kalshi inventory for %s %s (bot leg kept open)",
      side.upper(),
      ticker,
    )
    return store.log_trade({
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
      "status": "skipped",
      "detail": (
        f"Live EXIT skipped (no Kalshi inventory for {side.upper()} on {ticker}) — "
        f"bot leg kept open · {exit_reason}: {detail_suffix}{extra_detail}"
      ),
      "position_id": pos["id"],
    })

  sellable_before = sellable
  sell_count = float(contracts)
  if sellable is not None:
    if sellable > contracts + 0.05:
      entry_c = int(pos.get("entry_price_cents") or entry_c)
      synced = int(round(float(sellable)))
      store.update_position_contracts(
        str(pos["id"]),
        contracts=synced,
        cost_usd=round(synced * entry_c / 100.0, 2),
      )
      contracts = synced
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
  claimed_fill = int(exit_result.get("fill_count") or 0)
  sellable_after = kalshi_sellable_contracts(kalshi, ticker, side)
  fill_count = verify_kalshi_exit_fill(
    sellable_before=sellable_before,
    sellable_after=sellable_after,
    claimed_fill=claimed_fill,
  )
  if claimed_fill > 0 and fill_count <= 0:
    log.warning(
      "Live exit order %s claimed %s fills but Kalshi inventory unchanged on %s",
      live_exit_oid,
      claimed_fill,
      ticker,
    )
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
      "pnl_usd": 0,
      "signal": pick.get("signal"),
      "label": pos.get("label"),
      "status": "skipped",
      "detail": (
        f"Live EXIT unverified (API claimed {claimed_fill} fill(s) but Kalshi inventory unchanged) — "
        f"bot leg kept open · {exit_reason}: {detail_suffix}{extra_detail}"
      ),
      "position_id": pos["id"],
      "kalshi_order_id": live_exit_oid,
    })
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
    "sell_count": float(fill_count),
  }
