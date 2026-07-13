"""Per-event bot vs Kalshi P&L reconcile since stats epoch."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_event_time import canonical_hourly_event_ticker
from src.trading.kalshi_portfolio_pnl import (
  PNL_SOURCE_BOT_LOG,
  PNL_SOURCE_KALSHI_WALLET,
  kalshi_hourly_pnl_by_event_since,
)

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
    batch = kalshi_hourly_pnl_by_event_since(kalshi, since, asset=asset)
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
    "pnl_sources": {
      "bot": PNL_SOURCE_BOT_LOG,
      "kalshi": PNL_SOURCE_KALSHI_WALLET,
    },
    "epoch_start_at": since.isoformat(),
    "asset": asset,
    "totals": {
      "bot_pnl": round(total_bot, 2),
      "bot_pnl_source": PNL_SOURCE_BOT_LOG,
      "kalshi_pnl": kalshi_total,
      "kalshi_pnl_source": PNL_SOURCE_KALSHI_WALLET,
      "kalshi_pnl_per_event_sum": round(total_kalshi_rows, 2),
      "drift": round(total_bot - kalshi_total, 2),
      "kalshi_closed_trades": kalshi_closed,
      "note": "Kalshi totals use wallet settlement-net P&L (same engine as Kalshi P&L tab).",
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
