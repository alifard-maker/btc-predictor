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


def resting_sell_contracts(kalshi: Any, market_ticker: str, side: str) -> float:
  """Contracts tied up in unfilled resting sells for one leg."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0.0
  total = 0.0
  side_l = str(side or "").lower()
  ticker = str(market_ticker)
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "sell":
      continue
    if str(row.get("ticker") or "") != ticker:
      continue
    if str(row.get("side") or "").lower() != side_l:
      continue
    rem = row.get("remaining_count")
    if rem is None:
      rem = row.get("count")
    try:
      total += max(0.0, float(rem or 0))
    except (TypeError, ValueError):
      continue
  return total


def effective_kalshi_inventory(kalshi: Any, market_ticker: str, side: str) -> float | None:
  """Sellable inventory plus contracts in resting exit sells (API may report 0 while sell rests)."""
  sellable = kalshi_sellable_contracts(kalshi, market_ticker, side)
  if sellable is None:
    return None
  resting = resting_sell_contracts(kalshi, market_ticker, side)
  return max(float(sellable), float(resting))


def has_pending_bot_exit(kalshi: Any, store: Any, position_id: str) -> bool:
  pending_oid = resting_exit_order_id(store, position_id)
  return bool(pending_oid and order_still_resting(kalshi, pending_oid))


def should_reconcile_close_live_leg(kalshi: Any, store: Any, pos: dict[str, Any]) -> bool:
  """Only reconcile-close when Kalshi is truly flat (no position and no resting exit)."""
  if has_pending_bot_exit(kalshi, store, str(pos["id"])):
    return False
  ticker = str(pos["market_ticker"])
  side = str(pos["side"])
  if resting_sell_contracts(kalshi, ticker, side) > 0.05:
    return False
  sellable = kalshi_sellable_contracts(kalshi, ticker, side)
  return sellable is not None and sellable <= 0


def _inferred_exit_price_cents(store: Any, position_id: str) -> int | None:
  """Best-effort exit price from a recent resting/filled exit row for this leg."""
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT exit_price_cents, price_cents FROM bot_trades
      WHERE position_id = ? AND action = 'exit'
        AND status IN ('resting', 'filled')
        AND COALESCE(exit_price_cents, price_cents) IS NOT NULL
      ORDER BY created_at DESC LIMIT 1
      """,
      (position_id,),
    ).fetchone()
  if not row:
    return None
  val = row[0] if row[0] is not None else row[1]
  try:
    return int(val)
  except (TypeError, ValueError):
    return None


def reconcile_close_stale_live_leg(
  *,
  store: Any,
  pos: dict[str, Any],
  period_key: str,
  pick: dict[str, Any] | None = None,
  exit_reason: str = "RECONCILED",
  extra_detail: str = "",
) -> dict[str, Any]:
  """Close a bot leg when Kalshi has no inventory (settled, sold elsewhere, or already flat)."""
  from src.trading.paper_execution import leg_pnl_usd

  ticker = str(pos["market_ticker"])
  side = str(pos["side"])
  contracts = int(pos.get("contracts") or 0)
  entry_c = int(pos.get("entry_price_cents") or 0)
  pos_mode = normalize_position_mode(pos.get("mode"))
  pick = pick or {}
  exit_c = _inferred_exit_price_cents(store, str(pos["id"]))
  pnl_rounded = 0.0
  if exit_c is not None and entry_c and contracts:
    pnl_rounded = round(
      float(
        leg_pnl_usd(
          entry_price_cents=entry_c,
          mark_or_exit_cents=exit_c,
          contracts=contracts,
        )
        or 0.0,
      ),
      2,
    )
  store.close_position(str(pos["id"]))
  detail = f"Live EXIT reconciled"
  if exit_c is not None:
    detail += f" @ {exit_c}¢"
    if pnl_rounded < -0.005:
      detail += f" (loss ${abs(pnl_rounded):.2f})"
    elif pnl_rounded > 0.005:
      detail += f" (profit ${pnl_rounded:.2f})"
  detail += (
    f" (no Kalshi inventory for {side.upper()} on {ticker}) — "
    f"closed bot leg"
  )
  if extra_detail:
    detail += f" · {extra_detail}"
  log.info(
    "Reconciled closed stale live leg %s %s x%s on %s",
    side.upper(),
    ticker,
    contracts,
    period_key,
  )
  return store.log_trade({
    "event_ticker": period_key,
    "trigger": "continuous",
    "action": "exit",
    "mode": pos_mode,
    "market_ticker": ticker,
    "side": side,
    "contracts": contracts,
    "price_cents": exit_c if exit_c is not None else entry_c,
    "entry_price_cents": entry_c,
    "exit_price_cents": exit_c,
    "cost_usd": 0,
    "pnl_usd": pnl_rounded,
    "signal": pick.get("signal"),
    "label": pos.get("label"),
    "status": "reconciled",
    "detail": detail,
    "position_id": pos["id"],
  })


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
    sellable = effective_kalshi_inventory(kalshi, ticker, side)
    if sellable is None:
      continue
    bot_total = sum(int(p.get("contracts") or 0) for p in legs)
    target = int(round(float(sellable)))
    if abs(float(sellable) - float(bot_total)) < 0.05:
      continue

    if target <= 0:
      for pos in legs:
        if not should_reconcile_close_live_leg(kalshi, store, pos):
          changes.append({
            "ticker": ticker,
            "side": side,
            "action": "inventory_pending_exit",
            "position_id": str(pos["id"]),
            "bot_contracts": int(pos.get("contracts") or 0),
          })
          continue
        reconcile_close_stale_live_leg(
          store=store,
          pos=pos,
          period_key=event_ticker,
        )
        changes.append({
          "ticker": ticker,
          "side": side,
          "action": "reconciled_closed",
          "position_id": str(pos["id"]),
          "bot_contracts": int(pos.get("contracts") or 0),
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
    if resting_sell_contracts(kalshi, ticker, side) > 0.05:
      return None
    if not should_reconcile_close_live_leg(kalshi, store, pos):
      return None
    return reconcile_close_stale_live_leg(
      store=store,
      pos=pos,
      period_key=period_key,
      pick=pick,
      exit_reason=exit_reason,
      extra_detail=f"{exit_reason}: {detail_suffix}{extra_detail}",
    )

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
