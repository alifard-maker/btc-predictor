"""Import missing live hourly trades from Kalshi fill history."""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from src.data.kalshi import position_net_from_row
from src.trading.hourly_event_time import is_kalshi_hourly_event, market_ticker_event_ticker, hourly_fill_belongs_to_asset
from src.trading.kalshi_leg_exit import leg_price_cents_from_fill
from src.trading.paper_execution import leg_pnl_usd

log = logging.getLogger(__name__)

_FILL_BACKFILL_INTERVAL_SEC = 45.0
_last_fill_backfill_mono: dict[str, float] = {}


def _fill_created_at(fill: dict[str, Any]) -> datetime | None:
  raw = fill.get("created_time") or fill.get("ts") or fill.get("created_at")
  if not raw:
    return None
  try:
    if isinstance(raw, (int, float)):
      return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except (TypeError, ValueError, OSError):
    return None


def _fill_count(fill: dict[str, Any]) -> float:
  for key in ("count_fp", "count", "fill_count"):
    raw = fill.get(key)
    if raw is None or raw == "":
      continue
    try:
      val = float(raw)
    except (TypeError, ValueError):
      continue
    if val > 0:
      return val
  return 0.0


def _fill_market_ticker(fill: dict[str, Any]) -> str:
  return str(fill.get("ticker") or fill.get("market_ticker") or "").strip()


def _order_action_side(order: dict[str, Any]) -> tuple[str, str] | None:
  """Normalize Kalshi V1/V2 order rows to (action, held_side)."""
  action = str(order.get("action") or "").lower()
  side = str(order.get("side") or order.get("outcome_side") or "").lower()
  book = str(order.get("book_side") or "").lower()
  if side in ("bid", "ask"):
    book, side = side, ""
  if side not in ("yes", "no"):
    if book == "bid":
      side = "yes"
    elif book == "ask":
      side = "no"
  if action in ("buy", "sell") and side in ("yes", "no"):
    return action, side
  if book == "bid":
    return "buy", "yes"
  if book == "ask":
    return "buy", "no"
  return None


_ORDER_DIRECTION_CACHE: dict[int, tuple[float, dict[str, tuple[str, str]]]] = {}
_ORDER_CACHE_TTL_SEC = 120.0


def _build_order_direction_cache(kalshi: Any) -> dict[str, tuple[str, str]]:
  """Map order_id → (buy|sell, yes|no) for fills missing legacy action/side."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {}
  cache_key = id(kalshi)
  now = time.monotonic()
  cached = _ORDER_DIRECTION_CACHE.get(cache_key)
  if cached is not None and (now - cached[0]) < _ORDER_CACHE_TTL_SEC:
    return cached[1]
  cache: dict[str, tuple[str, str]] = {}
  for status in ("executed", "canceled", "resting"):
    cursor: str | None = None
    for _ in range(6):
      params: dict[str, Any] = {"status": status, "limit": 200}
      if cursor:
        params["cursor"] = cursor
      try:
        data = kalshi.get("/portfolio/orders", params=params, auth=True, critical=True)
      except Exception as e:
        log.warning("Kalshi order list (%s) failed: %s", status, e)
        break
      orders = data.get("orders") if isinstance(data, dict) else None
      if not isinstance(orders, list):
        break
      for order in orders:
        if not isinstance(order, dict):
          continue
        pair = _order_action_side(order)
        oid = order.get("order_id")
        if oid and pair:
          cache[str(oid)] = pair
      cursor = data.get("cursor") if isinstance(data, dict) else None
      if not cursor:
        break
  _ORDER_DIRECTION_CACHE[cache_key] = (now, cache)
  return cache


def _fill_action_side(
  fill: dict[str, Any],
  order_cache: dict[str, tuple[str, str]],
) -> tuple[str, str, str] | None:
  """Return (ticker, action, side) for one Kalshi fill row."""
  ticker = _fill_market_ticker(fill)
  if not ticker:
    return None
  action = str(fill.get("action") or "").lower()
  side = str(fill.get("side") or fill.get("outcome_side") or "").lower()
  book = str(fill.get("book_side") or "").lower()
  if side in ("bid", "ask"):
    book, side = side, ""
  if side not in ("yes", "no"):
    if book == "bid":
      side = "yes"
    elif book == "ask":
      side = "no"
  if action in ("buy", "sell") and side in ("yes", "no"):
    return ticker, action, side
  oid = str(fill.get("order_id") or "")
  if oid and oid in order_cache:
    act, sd = order_cache[oid]
    return ticker, act, sd
  return None


def _aggregate_fills_to_orders(
  fills: list[dict[str, Any]],
  *,
  order_cache: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
  """Merge partial fills per Kalshi order_id."""
  order_cache = order_cache or {}
  buckets: dict[str, dict[str, Any]] = {}
  skipped = 0
  for fill in fills:
    leg = _fill_action_side(fill, order_cache)
    if not leg:
      skipped += 1
      continue
    ticker, action, side = leg
    oid = str(fill.get("order_id") or fill.get("trade_id") or fill.get("fill_id") or "")
    if not oid:
      ts = _fill_created_at(fill)
      oid = f"anon:{ticker}:{action}:{side}:{ts.isoformat() if ts else 'unknown'}"
    ct = _fill_count(fill)
    if ct <= 0:
      continue
    px = leg_price_cents_from_fill(fill, held_side=side)
    if px is None:
      skipped += 1
      continue
    row = buckets.setdefault(
      oid,
      {
        "order_id": oid,
        "ticker": ticker,
        "action": action,
        "side": side,
        "contracts": 0.0,
        "price_value": 0.0,
        "created_at": _fill_created_at(fill),
      },
    )
    row["contracts"] += ct
    row["price_value"] += ct * float(px)
    ts = _fill_created_at(fill)
    if ts and (row["created_at"] is None or ts < row["created_at"]):
      row["created_at"] = ts

  out: list[dict[str, Any]] = []
  for row in buckets.values():
    if row["contracts"] < 0.05:
      continue
    row["price_cents"] = max(1, min(99, int(round(row["price_value"] / row["contracts"]))))
    out.append(row)
  out.sort(key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc))
  if skipped and not out:
    log.warning("Kalshi fill aggregate: skipped %s fill row(s) — missing direction or price", skipped)
  return out


def _kalshi_fill_action_to_bot(action: str) -> str:
  a = str(action or "").lower()
  if a == "buy":
    return "enter"
  if a == "sell":
    return "exit"
  return a


def _known_live_order_actions(store: Any) -> set[tuple[str, str]]:
  with store._connect() as conn:
    rows = conn.execute(
      """
      SELECT kalshi_order_id, action FROM bot_trades
      WHERE mode = 'live' AND kalshi_order_id IS NOT NULL
        AND status IN ('filled', 'reconciled')
      """,
    ).fetchall()
  return {
    (str(r[0]), str(r[1]).lower())
    for r in rows
    if r[0] and r[1]
  }


def _resting_enter_for_order(store: Any, order_id: str) -> dict[str, Any] | None:
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT * FROM bot_trades
      WHERE kalshi_order_id = ? AND action = 'enter' AND status = 'resting' AND mode = 'live'
      ORDER BY created_at DESC LIMIT 1
      """,
      (order_id,),
    ).fetchone()
  return dict(row) if row else None


def _open_live_position(store: Any, ticker: str, side: str) -> dict[str, Any] | None:
  side_l = str(side).lower()
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT * FROM bot_positions
      WHERE status = 'open' AND market_ticker = ? AND side = ? AND mode = 'live'
      ORDER BY opened_at DESC LIMIT 1
      """,
      (ticker, side_l),
    ).fetchone()
  return dict(row) if row else None


def _should_run_fill_backfill(store: Any, *, force: bool = False) -> bool:
  if force:
    return True
  key = str(getattr(store, "db_path", id(store)))
  now = time.monotonic()
  last = _last_fill_backfill_mono.get(key, 0.0)
  if now - last < _FILL_BACKFILL_INTERVAL_SEC:
    return False
  _last_fill_backfill_mono[key] = now
  return True


def backfill_kalshi_hourly_fills(
  store: Any,
  kalshi: Any,
  *,
  hours: float = 36.0,
  critical: bool = True,
  force: bool = False,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
  asset: str | None = None,
  order_cache: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
  """Replay recent Kalshi fills into the bot trade log for missing live enters/exits."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": True, "changes": [], "skipped": "kalshi_not_authenticated"}
  if not _should_run_fill_backfill(store, force=force):
    return {"ok": True, "changes": [], "skipped": "throttled"}

  from src.trading.bot_live_exit import cap_adopted_contracts

  cutoff = datetime.now(timezone.utc) - timedelta(hours=float(hours))
  raw_fills = kalshi.list_fills(limit=500, critical=critical)
  if order_cache is None:
    order_cache = _build_order_direction_cache(kalshi)
  hourly_fills: list[dict[str, Any]] = []
  for fill in raw_fills:
    ticker = _fill_market_ticker(fill)
    leg_event = market_ticker_event_ticker(ticker)
    if not leg_event or not is_kalshi_hourly_event(leg_event):
      continue
    if asset and not hourly_fill_belongs_to_asset(ticker, asset):
      continue
    ts = _fill_created_at(fill)
    if ts and ts < cutoff:
      continue
    hourly_fills.append(fill)

  orders = _aggregate_fills_to_orders(hourly_fills, order_cache=order_cache)
  known = _known_live_order_actions(store)
  changes: list[dict[str, Any]] = []

  for order in orders:
    oid = str(order["order_id"])
    action = str(order["action"])
    bot_action = _kalshi_fill_action_to_bot(action)
    if (oid, bot_action) in known:
      continue

    ticker = str(order["ticker"])
    side = str(order["side"])
    leg_event = market_ticker_event_ticker(ticker) or ""
    contracts_fp = float(order["contracts"])
    price_cents = int(order["price_cents"])
    created_at = order.get("created_at")
    created_iso = created_at.isoformat() if isinstance(created_at, datetime) else None

    if action == "buy":
      resting = _resting_enter_for_order(store, oid)
      if resting and hasattr(store, "promote_resting_enter_to_filled"):
        contracts, contracts_fp = cap_adopted_contracts(contracts_fp, cfg, kind=kind)
        pid = str(uuid.uuid4())
        cost_usd = round(contracts_fp * price_cents / 100.0, 2)
        detail = (
          f"Live ENTER backfilled from Kalshi fills "
          f"(order {oid}) — {contracts} contracts"
        )
        if _open_live_position(store, ticker, side):
          existing = _open_live_position(store, ticker, side)
          store.promote_resting_enter_to_filled(
            resting["id"],
            event_ticker=leg_event,
            contracts=contracts,
            cost_usd=cost_usd,
            entry_price_cents=price_cents,
            position_id=str(existing["id"]) if existing else pid,
            detail=detail,
          )
          changes.append({"action": "promoted_resting_from_fills", "order_id": oid, "ticker": ticker})
          known.add((oid, "enter"))
          continue
        store.open_position({
          "id": pid,
          "event_ticker": leg_event,
          "market_ticker": ticker,
          "side": side,
          "contracts": contracts,
          "contracts_fp": contracts_fp,
          "entry_price_cents": price_cents,
          "cost_usd": cost_usd,
          "label": resting.get("label"),
          "signal": resting.get("signal"),
          "mode": "live",
          "entry_source": "kalshi_fill_backfill",
        })
        store.promote_resting_enter_to_filled(
          resting["id"],
          event_ticker=leg_event,
          contracts=contracts,
          cost_usd=cost_usd,
          entry_price_cents=price_cents,
          position_id=pid,
          detail=detail,
        )
        changes.append({"action": "promoted_resting_from_fills", "order_id": oid, "ticker": ticker})
        known.add((oid, "enter"))
        continue

      if _open_live_position(store, ticker, side) or _has_filled_enter_for_order(store, oid):
        known.add((oid, "enter"))
        continue

      contracts, contracts_fp = cap_adopted_contracts(contracts_fp, cfg, kind=kind)
      pid = str(uuid.uuid4())
      cost_usd = round(contracts_fp * price_cents / 100.0, 2)
      detail = (
        f"Live ENTER backfilled from Kalshi fills "
        f"(order {oid}) — {contracts} contracts"
      )
      store.open_position({
        "id": pid,
        "event_ticker": leg_event,
        "market_ticker": ticker,
        "side": side,
        "contracts": contracts,
        "contracts_fp": contracts_fp,
        "entry_price_cents": price_cents,
        "cost_usd": cost_usd,
        "mode": "live",
        "entry_source": "kalshi_fill_backfill",
      })
      store.log_trade({
        "event_ticker": leg_event,
        "trigger": "kalshi_fill_sync",
        "action": "enter",
        "mode": "live",
        "market_ticker": ticker,
        "side": side,
        "contracts": contracts,
        "price_cents": price_cents,
        "entry_price_cents": price_cents,
        "cost_usd": cost_usd,
        "status": "filled",
        "detail": detail,
        "position_id": pid,
        "kalshi_order_id": oid,
        "created_at": created_iso,
      })
      changes.append({"action": "backfilled_enter", "order_id": oid, "ticker": ticker})
      known.add((oid, "enter"))
      continue

    # sell
    pos = _open_live_position(store, ticker, side)
    if not pos:
      known.add((oid, "exit"))
      continue

    contracts = int(pos.get("contracts") or 0)
    entry_c = int(pos.get("entry_price_cents") or 0)
    sell_ct = min(contracts, max(1, int(round(contracts_fp))))
    pnl = round(
      float(
        leg_pnl_usd(
          entry_price_cents=entry_c,
          mark_or_exit_cents=price_cents,
          contracts=sell_ct,
        )
        or 0.0,
      ),
      2,
    )
    store.close_position(str(pos["id"]))
    detail = (
      f"Live EXIT backfilled from Kalshi fills "
      f"(order {oid}) — {side.upper()} x{sell_ct} @ {price_cents}¢ "
      f"(entry {entry_c}¢) — {'+' if pnl >= 0 else ''}${pnl:.2f}"
    )
    store.log_trade({
      "event_ticker": str(pos.get("event_ticker") or leg_event),
      "trigger": "kalshi_fill_sync",
      "action": "exit",
      "mode": "live",
      "market_ticker": ticker,
      "side": side,
      "contracts": sell_ct,
      "price_cents": price_cents,
      "entry_price_cents": entry_c,
      "exit_price_cents": price_cents,
      "pnl_usd": pnl,
      "label": pos.get("label"),
      "status": "filled",
      "detail": detail,
      "position_id": pos.get("id"),
      "kalshi_order_id": oid,
      "created_at": created_iso,
    })
    changes.append({"action": "backfilled_exit", "order_id": oid, "ticker": ticker, "pnl_usd": pnl})
    known.add((oid, "exit"))

  if changes:
    log.info("Kalshi fill backfill: %s change(s)", len(changes))
  return {
    "ok": True,
    "changes": changes,
    "orders_scanned": len(orders),
    "fills_seen": len(hourly_fills),
  }


def _has_filled_enter_for_order(store: Any, order_id: str) -> bool:
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT 1 FROM bot_trades
      WHERE kalshi_order_id = ? AND action = 'enter'
        AND mode = 'live' AND status = 'filled'
      LIMIT 1
      """,
      (order_id,),
    ).fetchone()
  return row is not None


def replay_closed_legs_from_kalshi_fills(
  store: Any,
  kalshi: Any,
  *,
  hours: float = 36.0,
  critical: bool = True,
  order_cache: dict[str, tuple[str, str]] | None = None,
  asset: str | None = None,
) -> dict[str, Any]:
  """
  Second pass: pair buy+sell orders on the same leg when no open position exists.

  Handles fully closed Kalshi round-trips that never touched the bot DB.
  """
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": True, "changes": []}

  cutoff = datetime.now(timezone.utc) - timedelta(hours=float(hours))
  raw_fills = kalshi.list_fills(limit=500, critical=critical)
  if order_cache is None:
    order_cache = _build_order_direction_cache(kalshi)
  hourly_fills = [
    f for f in raw_fills
    if market_ticker_event_ticker(_fill_market_ticker(f))
    and is_kalshi_hourly_event(market_ticker_event_ticker(_fill_market_ticker(f)) or "")
    and (not asset or hourly_fill_belongs_to_asset(_fill_market_ticker(f), asset))
    and (_fill_created_at(f) is None or _fill_created_at(f) >= cutoff)
  ]
  orders = _aggregate_fills_to_orders(hourly_fills, order_cache=order_cache)
  known = _known_live_order_actions(store)
  changes: list[dict[str, Any]] = []

  # Group by (ticker, side)
  legs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
  for order in orders:
    legs[(str(order["ticker"]), str(order["side"]))].append(order)

  for (ticker, side), leg_orders in legs.items():
    leg_event = market_ticker_event_ticker(ticker) or ""
    if not leg_event:
      continue
    buys = [o for o in leg_orders if o["action"] == "buy"]
    sells = [o for o in leg_orders if o["action"] == "sell"]
    for buy in buys:
      oid_buy = str(buy["order_id"])
      if (oid_buy, "enter") in known:
        continue
      # Skip if any filled enter exists for this leg in window
      if _has_filled_enter_for_order(store, oid_buy):
        known.add((oid_buy, "enter"))
        continue
      # Find matching sell after buy
      buy_time = buy.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
      sell = next(
        (
          s for s in sells
          if (str(s["order_id"]), "exit") not in known
          and (s.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)) >= buy_time
        ),
        None,
      )
      if not sell:
        continue
      oid_sell = str(sell["order_id"])
      contracts = max(1, int(round(min(float(buy["contracts"]), float(sell["contracts"])))))
      entry_c = int(buy["price_cents"])
      exit_c = int(sell["price_cents"])
      pnl = round(
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
      buy_iso = buy.get("created_at").isoformat() if buy.get("created_at") else None
      sell_iso = sell.get("created_at").isoformat() if sell.get("created_at") else None
      pid = str(uuid.uuid4())
      store.log_trade({
        "event_ticker": leg_event,
        "trigger": "kalshi_fill_sync",
        "action": "enter",
        "mode": "live",
        "market_ticker": ticker,
        "side": side,
        "contracts": contracts,
        "price_cents": entry_c,
        "entry_price_cents": entry_c,
        "cost_usd": round(contracts * entry_c / 100.0, 2),
        "status": "filled",
        "detail": f"Live ENTER backfilled from Kalshi fills (closed leg, order {oid_buy})",
        "position_id": pid,
        "kalshi_order_id": oid_buy,
        "created_at": buy_iso,
      })
      store.log_trade({
        "event_ticker": leg_event,
        "trigger": "kalshi_fill_sync",
        "action": "exit",
        "mode": "live",
        "market_ticker": ticker,
        "side": side,
        "contracts": contracts,
        "price_cents": exit_c,
        "entry_price_cents": entry_c,
        "exit_price_cents": exit_c,
        "pnl_usd": pnl,
        "status": "filled",
        "detail": (
          f"Live EXIT backfilled from Kalshi fills (closed leg, order {oid_sell}) "
          f"— {'+' if pnl >= 0 else ''}${pnl:.2f}"
        ),
        "position_id": pid,
        "kalshi_order_id": oid_sell,
        "created_at": sell_iso,
      })
      known.add((oid_buy, "enter"))
      known.add((oid_sell, "exit"))
      changes.append({
        "action": "backfilled_closed_round_trip",
        "ticker": ticker,
        "side": side,
        "pnl_usd": pnl,
      })

  return {"ok": True, "changes": changes}


def sync_kalshi_fills_to_store(
  store: Any,
  kalshi: Any,
  *,
  hours: float = 36.0,
  critical: bool = True,
  force: bool = False,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
  asset: str | None = None,
) -> dict[str, Any]:
  """Run fill backfill passes (open legs, then closed round-trips)."""
  order_cache = _build_order_direction_cache(kalshi)
  first = backfill_kalshi_hourly_fills(
    store, kalshi, hours=hours, critical=critical, force=force, cfg=cfg, kind=kind,
    asset=asset, order_cache=order_cache,
  )
  second = replay_closed_legs_from_kalshi_fills(
    store, kalshi, hours=hours, critical=critical, order_cache=order_cache, asset=asset,
  )
  changes = (first.get("changes") or []) + (second.get("changes") or [])
  return {
    "ok": True,
    "changes": changes,
    "orders_scanned": first.get("orders_scanned", 0),
    "fills_seen": first.get("fills_seen", 0),
    "skipped": first.get("skipped"),
  }


def summarize_kalshi_experiment_fills(
  kalshi: Any,
  *,
  since: datetime,
  critical: bool = True,
  max_fills: int = 1000,
  asset: str | None = None,
) -> dict[str, Any]:
  """
  Realized P&L from Kalshi hourly fill history since an instant (exchange source of truth).
  Pairs buy+sell on the same ticker/side; open legs are excluded from closed P&L.
  """
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": False, "error": "Kalshi not authenticated"}

  raw_fills = kalshi.list_fills(limit=min(max_fills, 1000), critical=critical)
  order_cache = _build_order_direction_cache(kalshi)
  hourly_fills = [
    f for f in raw_fills
    if market_ticker_event_ticker(_fill_market_ticker(f))
    and is_kalshi_hourly_event(market_ticker_event_ticker(_fill_market_ticker(f)) or "")
    and (not asset or hourly_fill_belongs_to_asset(_fill_market_ticker(f), asset))
    and (_fill_created_at(f) is None or _fill_created_at(f) >= since)
  ]
  orders = _aggregate_fills_to_orders(hourly_fills, order_cache=order_cache)
  legs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
  for order in orders:
    legs[(str(order["ticker"]), str(order["side"]))].append(order)

  closed_pnls: list[float] = []
  wins = 0
  losses = 0
  for leg_orders in legs.values():
    buys = sorted(
      [o for o in leg_orders if o["action"] == "buy"],
      key=lambda o: o.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    sells = sorted(
      [o for o in leg_orders if o["action"] == "sell"],
      key=lambda o: o.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    sell_i = 0
    for buy in buys:
      buy_time = buy.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
      while sell_i < len(sells):
        sell = sells[sell_i]
        sell_time = sell.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
        if sell_time < buy_time:
          sell_i += 1
          continue
        contracts = max(1, int(round(min(float(buy["contracts"]), float(sell["contracts"])))))
        entry_c = int(buy["price_cents"])
        exit_c = int(sell["price_cents"])
        pnl = round(
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
        closed_pnls.append(pnl)
        if pnl > 0:
          wins += 1
        elif pnl < 0:
          losses += 1
        sell_i += 1
        break
      else:
        break

  total = round(sum(closed_pnls), 2)
  n = len(closed_pnls)
  out: dict[str, Any] = {
    "ok": True,
    "source": "kalshi_fills",
    "closed_trades": n,
    "total_pnl_usd": total,
    "wins": wins,
    "losses": losses,
    "fills_scanned": len(hourly_fills),
  }
  if n:
    out["win_rate"] = round(wins / n, 3)
    out["avg_pnl_usd"] = round(total / n, 2)
  return out
