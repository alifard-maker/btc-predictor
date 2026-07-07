"""Per-event bot vs Kalshi P&L reconcile since stats epoch."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_event_time import canonical_hourly_event_ticker


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


def _kalshi_events_since_epoch(kalshi: Any, since: datetime, *, asset: str) -> set[str]:
  from src.trading.hourly_event_time import (
    hourly_fill_belongs_to_asset,
    is_kalshi_hourly_event,
    market_ticker_event_ticker,
  )
  from src.trading.kalshi_fill_sync import _fill_created_at, _fill_market_ticker

  events: set[str] = set()
  for fill in kalshi.list_fills(limit=1000, critical=True):
    ticker = _fill_market_ticker(fill)
    leg_event = market_ticker_event_ticker(ticker)
    if not leg_event or not is_kalshi_hourly_event(leg_event):
      continue
    if not hourly_fill_belongs_to_asset(ticker, asset):
      continue
    ts = _fill_created_at(fill)
    if ts is None or ts < since:
      continue
    events.add(canonical_hourly_event_ticker(leg_event))
  return events


def _kalshi_pnl_by_event(
  kalshi: Any,
  since: datetime,
  *,
  asset: str,
  events: set[str],
) -> dict[str, dict[str, Any]]:
  from src.trading.kalshi_fill_sync import summarize_kalshi_experiment_fills

  by_event: dict[str, dict[str, Any]] = {}
  for event in sorted(events):
    sm = summarize_kalshi_experiment_fills(
      kalshi,
      since=since,
      asset=asset,
      event_ticker=event,
    )
    if not sm.get("ok"):
      continue
    by_event[event] = {
      "kalshi_pnl": round(float(sm.get("total_pnl_usd") or 0), 2),
      "kalshi_closed": int(sm.get("closed_trades") or 0),
    }
  return by_event


def build_epoch_reconcile_report(
  loop: Any,
  cfg: dict[str, Any] | None,
  asset: str = "btc",
) -> dict[str, Any]:
  """Bot vs Kalshi realized P&L per hourly event since stats epoch."""
  from src.trading.pnl_first_railway_manager import _stats_epoch

  since = _stats_epoch(cfg)
  store = loop.hourly_bot_store(asset, kind="hourly")
  bot_by_event = _bot_pnl_by_event(store, since)

  kalshi_by_event: dict[str, dict[str, Any]] = {}
  kalshi = loop._kalshi_for(asset) if hasattr(loop, "_kalshi_for") else getattr(loop, "kalshi", None)
  if kalshi and getattr(kalshi, "authenticated", False):
    events = set(bot_by_event) | _kalshi_events_since_epoch(kalshi, since, asset=asset)
    kalshi_by_event = _kalshi_pnl_by_event(kalshi, since, asset=asset, events=events)

  rows: list[dict[str, Any]] = []
  all_events = sorted(set(bot_by_event) | set(kalshi_by_event))
  total_bot = 0.0
  total_kalshi = 0.0
  for event in all_events:
    bot_row = bot_by_event.get(event, {})
    kalshi_row = kalshi_by_event.get(event, {})
    bot_pnl = round(float(bot_row.get("bot_pnl") or 0), 2)
    kalshi_pnl = round(float(kalshi_row.get("kalshi_pnl") or 0), 2)
    drift = round(bot_pnl - kalshi_pnl, 2)
    total_bot += bot_pnl
    total_kalshi += kalshi_pnl
    rows.append({
      "event_ticker": event,
      "bot_pnl": bot_pnl,
      "kalshi_pnl": kalshi_pnl,
      "drift": drift,
      "bot_trades": int(bot_row.get("bot_trades") or 0),
      "kalshi_closed": int(kalshi_row.get("kalshi_closed") or 0),
    })

  return {
    "ok": True,
    "epoch_start_at": since.isoformat(),
    "asset": asset,
    "totals": {
      "bot_pnl": round(total_bot, 2),
      "kalshi_pnl": round(total_kalshi, 2),
      "drift": round(total_bot - total_kalshi, 2),
    },
    "rows": rows,
  }
