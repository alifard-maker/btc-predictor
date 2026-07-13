"""Per-event bot vs Kalshi P&L reconcile since stats epoch."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_event_time import canonical_hourly_event_ticker

_REPORT_CACHE: dict[str, Any] = {"mono_at": 0.0, "payload": None, "key": None}
_REPORT_CACHE_TTL_SEC = 90.0


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=timezone.utc)
    return dt
  except ValueError:
    return None


def _bot_pnl_by_event(store: Any, since: datetime) -> dict[str, dict[str, Any]]:
  from src.trading.bot_exit_pnl import effective_exit_pnl_usd

  by_event: dict[str, dict[str, Any]] = defaultdict(lambda: {"bot_pnl": 0.0, "bot_trades": 0})
  for trade in store.list_trades(limit=5000):
    if str(trade.get("action") or "") != "exit":
      continue
    if str(trade.get("status") or "") not in ("filled", "reconciled"):
      continue
    if str(trade.get("mode") or "").lower() != "live":
      continue
    created = _parse_ts(trade.get("created_at"))
    if created is None or created < since:
      continue
    event = canonical_hourly_event_ticker(str(trade.get("event_ticker") or ""))
    if not event:
      continue
    by_event[event]["bot_pnl"] = round(
      float(by_event[event]["bot_pnl"]) + float(effective_exit_pnl_usd(trade) or 0),
      2,
    )
    by_event[event]["bot_trades"] = int(by_event[event]["bot_trades"]) + 1
  return dict(by_event)


def _kalshi_hourly_closed_batched(
  kalshi: Any,
  since: datetime,
  *,
  asset: str,
) -> dict[str, Any]:
  """One Kalshi fills+settlements fetch; per-event and global closed P&L."""
  from src.trading.hourly_event_time import (
    hourly_fill_belongs_to_asset,
    is_kalshi_hourly_event,
    market_ticker_event_ticker,
  )
  from src.trading.kalshi_fill_sync import (
    _aggregate_fills_to_orders,
    _aggregate_settlements_to_exits,
    _build_order_direction_cache,
    _fill_created_at,
    _fill_market_ticker,
  )
  from src.trading.paper_execution import leg_pnl_usd

  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {
      "ok": False,
      "error": "Kalshi not authenticated",
      "by_event": {},
      "summary": None,
    }

  raw_fills = kalshi.list_fills(limit=1000, critical=True)
  raw_settlements = kalshi.list_settlements(limit=1000, critical=True)
  order_cache = _build_order_direction_cache(kalshi)
  hourly_all: list[dict[str, Any]] = []
  for fill in raw_fills:
    ticker = _fill_market_ticker(fill)
    leg_event = market_ticker_event_ticker(ticker)
    if not leg_event or not is_kalshi_hourly_event(leg_event):
      continue
    if not hourly_fill_belongs_to_asset(ticker, asset):
      continue
    hourly_all.append(fill)

  settlement_exits = _aggregate_settlements_to_exits(raw_settlements, asset=asset)
  orders = _aggregate_fills_to_orders(hourly_all, order_cache=order_cache)
  legs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
  for order in orders:
    legs[(str(order["ticker"]), str(order["side"]))].append(order)

  settlement_by_leg: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
  for exit_row in settlement_exits:
    settlement_by_leg[(str(exit_row["ticker"]), str(exit_row["side"]))].append(exit_row)

  by_event: dict[str, dict[str, Any]] = defaultdict(lambda: {"kalshi_pnl": 0.0, "kalshi_closed": 0})
  closed_pnls: list[float] = []
  wins = 0
  losses = 0

  for (ticker, side), leg_orders in legs.items():
    event = canonical_hourly_event_ticker(market_ticker_event_ticker(ticker))
    if not event:
      continue
    buys = sorted(
      [o for o in leg_orders if o["action"] == "buy"],
      key=lambda o: o.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    sells = sorted(
      [o for o in leg_orders if o["action"] == "sell"],
      key=lambda o: o.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    leg_settlements = settlement_by_leg.get((ticker, side), [])
    exits = sorted(
      sells + leg_settlements,
      key=lambda o: o.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    epoch_buys = sorted(
      [
        b for b in buys
        if (b.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)) >= since
      ],
      key=lambda o: o.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    epoch_exits = sorted(
      [
        s for s in exits
        if (s.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)) >= since
      ],
      key=lambda o: o.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    sell_i = 0
    for buy in epoch_buys:
      buy_time = buy.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
      while sell_i < len(epoch_exits):
        sell = epoch_exits[sell_i]
        sell_time = sell.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
        if sell_time < buy_time:
          sell_i += 1
          continue
        contracts = max(1, int(round(min(float(buy["contracts"]), float(sell["contracts"])))))
        entry_c = int(buy["price_cents"])
        exit_c = int(sell["price_cents"])
        sell_i += 1
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
        by_event[event]["kalshi_pnl"] = round(float(by_event[event]["kalshi_pnl"]) + pnl, 2)
        by_event[event]["kalshi_closed"] = int(by_event[event]["kalshi_closed"]) + 1
        if pnl > 0:
          wins += 1
        elif pnl < 0:
          losses += 1
        break

  total = round(sum(closed_pnls), 2)
  n = len(closed_pnls)
  summary = {
    "ok": True,
    "source": "kalshi_fills",
    "closed_trades": n,
    "total_pnl_usd": total,
    "wins": wins,
    "losses": losses,
    "fills_total": len(hourly_all),
  }
  return {
    "ok": True,
    "by_event": dict(by_event),
    "summary": summary,
  }


def build_epoch_reconcile_report(
  loop: Any,
  cfg: dict[str, Any] | None,
  asset: str = "btc",
) -> dict[str, Any]:
  """Bot vs Kalshi realized P&L per hourly event since stats epoch."""
  from src.trading.pnl_first_railway_manager import experiment_epoch_at

  since = experiment_epoch_at(loop, cfg, asset=asset)
  store = loop.hourly_bot_store(asset, kind="hourly")
  bot_by_event = _bot_pnl_by_event(store, since)

  kalshi_by_event: dict[str, dict[str, Any]] = {}
  kalshi_summary: dict[str, Any] | None = None
  kalshi = loop._kalshi_for(asset) if hasattr(loop, "_kalshi_for") else getattr(loop, "kalshi", None)
  if kalshi and getattr(kalshi, "authenticated", False):
    batch = _kalshi_hourly_closed_batched(kalshi, since, asset=asset)
    if batch.get("ok"):
      kalshi_by_event = batch.get("by_event") or {}
      kalshi_summary = batch.get("summary")

  rows: list[dict[str, Any]] = []
  all_events = sorted(set(bot_by_event) | set(kalshi_by_event))
  total_bot = 0.0
  total_kalshi_rows = 0.0

  for event in all_events:
    bot_row = bot_by_event.get(event, {})
    kalshi_row = kalshi_by_event.get(event, {})
    bot_pnl = round(float(bot_row.get("bot_pnl") or 0), 2)
    kalshi_pnl = round(float(kalshi_row.get("kalshi_pnl") or 0), 2)
    drift = round(bot_pnl - kalshi_pnl, 2)
    total_bot += bot_pnl
    total_kalshi_rows += kalshi_pnl
    rows.append({
      "event_ticker": event,
      "bot_pnl": bot_pnl,
      "kalshi_pnl": kalshi_pnl,
      "drift": drift,
      "bot_trades": int(bot_row.get("bot_trades") or 0),
      "kalshi_closed": int(kalshi_row.get("kalshi_closed") or 0),
    })

  kalshi_total = round(float((kalshi_summary or {}).get("total_pnl_usd") or total_kalshi_rows), 2)
  kalshi_closed = int((kalshi_summary or {}).get("closed_trades") or 0)

  return {
    "ok": True,
    "epoch_start_at": since.isoformat(),
    "asset": asset,
    "totals": {
      "bot_pnl": round(total_bot, 2),
      "kalshi_pnl": kalshi_total,
      "kalshi_pnl_per_event_sum": round(total_kalshi_rows, 2),
      "drift": round(total_bot - kalshi_total, 2),
      "kalshi_closed_trades": kalshi_closed,
      "note": "kalshi_pnl uses global fill pairing (ground truth); per-event rows may under-count settlements",
    },
    "kalshi_summary": kalshi_summary,
    "rows": rows,
  }


def build_epoch_reconcile_report_cached(
  loop: Any,
  cfg: dict[str, Any] | None,
  asset: str = "btc",
  *,
  ttl_sec: float = _REPORT_CACHE_TTL_SEC,
) -> dict[str, Any]:
  from src.trading.pnl_first_railway_manager import experiment_epoch_at

  since = experiment_epoch_at(loop, cfg, asset=asset)
  cache_key = f"{asset}:{since.isoformat()}"
  now = time.monotonic()
  if (
    _REPORT_CACHE.get("key") == cache_key
    and _REPORT_CACHE.get("payload")
    and (now - float(_REPORT_CACHE.get("mono_at") or 0)) < ttl_sec
  ):
    return {
      **(_REPORT_CACHE["payload"]),
      "cached": True,
      "cache_age_sec": round(now - float(_REPORT_CACHE["mono_at"]), 1),
    }

  payload = build_epoch_reconcile_report(loop, cfg, asset=asset)
  _REPORT_CACHE["key"] = cache_key
  _REPORT_CACHE["mono_at"] = now
  _REPORT_CACHE["payload"] = payload
  return {**payload, "cached": False, "cache_age_sec": 0.0}


def invalidate_epoch_reconcile_cache() -> None:
  _REPORT_CACHE["mono_at"] = 0.0
  _REPORT_CACHE["payload"] = None
  _REPORT_CACHE["key"] = None
