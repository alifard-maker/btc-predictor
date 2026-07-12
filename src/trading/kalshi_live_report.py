"""Kalshi-only live experiment report (ground truth fills, no bot mark P&L)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_event_time import hourly_event_settle_utc, market_ticker_event_ticker
from src.trading.kalshi_fill_sync import (
  _aggregate_fills_to_orders,
  _aggregate_settlements_to_exits,
  _build_order_direction_cache,
  _fill_created_at,
  _fill_market_ticker,
  summarize_kalshi_experiment_fills,
)
from src.trading.trade_timing_analytics import bucket_minutes_to_settle


def _closed_legs_from_kalshi(
  kalshi: Any,
  *,
  since: datetime,
  asset: str,
) -> list[dict[str, Any]]:
  from src.trading.hourly_event_time import hourly_fill_belongs_to_asset, is_kalshi_hourly_event
  from src.trading.paper_execution import leg_pnl_usd as paper_leg_pnl

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

  orders = _aggregate_fills_to_orders(hourly_all, order_cache=order_cache)
  settlement_exits = _aggregate_settlements_to_exits(raw_settlements, asset=asset)
  legs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
  for order in orders:
    legs[(str(order["ticker"]), str(order["side"]))].append(order)

  settlement_by_leg: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
  for exit_row in settlement_exits:
    settlement_by_leg[(str(exit_row["ticker"]), str(exit_row["side"]))].append(exit_row)

  closed: list[dict[str, Any]] = []
  for (ticker, side), leg_orders in legs.items():
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
    sell_i = 0
    epoch_buys = [
      b for b in buys
      if (b.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)) >= since
    ]
    for buy in sorted(
      epoch_buys,
      key=lambda o: o.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    ):
      buy_time = buy.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
      while sell_i < len(exits):
        sell = exits[sell_i]
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
            paper_leg_pnl(
              entry_price_cents=entry_c,
              mark_or_exit_cents=exit_c,
              contracts=contracts,
            )
            or 0.0,
          ),
          2,
        )
        event = market_ticker_event_ticker(str(buy.get("ticker") or ticker))
        settle = hourly_event_settle_utc(str(event)) if event else None
        entry_min = None
        exit_min = None
        if settle and buy_time:
          entry_min = round((settle - buy_time).total_seconds() / 60.0, 1)
        if settle and sell_time:
          exit_min = round((settle - sell_time).total_seconds() / 60.0, 1)
        exit_type = "SETTLEMENT" if sell.get("source") == "settlement" else "SELL"
        closed.append({
          "event_ticker": event,
          "market_ticker": ticker,
          "side": side,
          "pnl_usd": pnl,
          "contracts": contracts,
          "entry_at": buy_time.isoformat() if buy_time else None,
          "exit_at": sell_time.isoformat() if sell_time else None,
          "exit_type": exit_type,
          "entry_minutes_to_settle": entry_min,
          "exit_minutes_to_settle": exit_min,
          "entry_bucket": bucket_minutes_to_settle(entry_min),
          "exit_bucket": bucket_minutes_to_settle(exit_min),
        })
        break
  return closed


def _bucket_agg(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
  groups: dict[str, list[float]] = defaultdict(list)
  for row in rows:
    groups[str(row.get(field) or "unknown")].append(float(row.get("pnl_usd") or 0))
  out: list[dict[str, Any]] = []
  for bucket, pnls in sorted(groups.items(), key=lambda kv: kv[0]):
    n = len(pnls)
    total = round(sum(pnls), 2)
    wins = sum(1 for p in pnls if p > 0)
    out.append({
      "bucket": bucket,
      "trades": n,
      "wins": wins,
      "win_rate": round(wins / n, 3) if n else None,
      "total_pnl_usd": total,
      "avg_pnl_usd": round(total / n, 2) if n else None,
    })
  return out


def _by_event(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  groups: dict[str, list[float]] = defaultdict(list)
  for row in rows:
    event = str(row.get("event_ticker") or "unknown")
    groups[event].append(float(row.get("pnl_usd") or 0))
  out = []
  for event, pnls in groups.items():
    total = round(sum(pnls), 2)
    n = len(pnls)
    out.append({
      "event_ticker": event,
      "closed_legs": n,
      "total_pnl_usd": total,
      "avg_pnl_usd": round(total / n, 2) if n else None,
    })
  out.sort(key=lambda r: str(r.get("event_ticker") or ""))
  return out


def build_kalshi_live_report(
  loop: Any,
  cfg: dict[str, Any] | None,
  *,
  asset: str = "btc",
) -> dict[str, Any]:
  from src.trading.pnl_first_railway_manager import experiment_epoch_at

  since = experiment_epoch_at(loop, cfg, asset=asset)
  kalshi = loop._kalshi_for(asset) if hasattr(loop, "_kalshi_for") else getattr(loop, "kalshi", None)
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": False, "error": "Kalshi not authenticated", "asset": asset}

  summary = summarize_kalshi_experiment_fills(kalshi, since=since, asset=asset, critical=True)
  closed = _closed_legs_from_kalshi(kalshi, since=since, asset=asset)
  by_exit: dict[str, list[float]] = defaultdict(list)
  for row in closed:
    by_exit[str(row.get("exit_type") or "unknown")].append(float(row.get("pnl_usd") or 0))

  return {
    "ok": True,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "asset": asset,
    "epoch_start_at": since.isoformat(),
    "summary": summary,
    "closed_legs": int(summary.get("closed_trades") or len(closed)),
    "total_pnl_usd": round(float(summary.get("total_pnl_usd") or 0), 2),
    "by_event": _by_event(closed),
    "by_exit_type": [
      {
        "exit_type": k,
        "trades": len(v),
        "total_pnl_usd": round(sum(v), 2),
        "avg_pnl_usd": round(sum(v) / len(v), 2) if v else None,
      }
      for k, v in sorted(by_exit.items(), key=lambda kv: sum(kv[1]))
    ],
    "by_entry_timing": _bucket_agg(closed, "entry_bucket"),
    "by_exit_timing": _bucket_agg(closed, "exit_bucket"),
    "recent_legs": closed[-30:],
  }
