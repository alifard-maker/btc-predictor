"""Side-by-side comparison of live hourly bot vs paper hourly trial per event."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_bot_store import HourlyBotStore

_FILLED_ENTER = ("filled", "reconciled", "resting")
_REALIZED_EXIT = ("filled", "reconciled")
_EXIT_REASON_RE = re.compile(r"EXIT\s*\(([^)]+)\)", re.IGNORECASE)


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _exit_reason(trade: dict[str, Any]) -> str:
  ctx = trade.get("exit_context")
  if isinstance(ctx, dict):
    reason = ctx.get("exit_reason")
    if reason:
      return str(reason)
  detail = str(trade.get("detail") or "")
  m = _EXIT_REASON_RE.search(detail)
  if m:
    return m.group(1).strip()
  return detail.split(":")[0].strip() if detail else "—"


def _recent_event_tickers(
  store: HourlyBotStore,
  *,
  mode: str,
  limit: int,
) -> list[tuple[str, datetime]]:
  with store._connect() as conn:
    rows = conn.execute(
      f"""
      SELECT event_ticker, MAX(created_at) AS last_at
      FROM bot_trades
      WHERE event_ticker IS NOT NULL AND TRIM(event_ticker) != ''
        AND mode = ?
      GROUP BY event_ticker
      ORDER BY last_at DESC
      LIMIT ?
      """,
      (mode, limit),
    ).fetchall()
  out: list[tuple[str, datetime]] = []
  for row in rows:
    evt = str(row["event_ticker"])
    ts = _parse_ts(row["last_at"])
    if ts is not None:
      out.append((evt, ts))
  return out


def _filter_trades_by_mode(trades: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
  want = mode.lower()
  return [t for t in trades if str(t.get("mode") or "").lower() == want]


def _entry_row(trade: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": trade.get("id"),
    "created_at": trade.get("created_at"),
    "side": trade.get("side"),
    "market_ticker": trade.get("market_ticker"),
    "label": trade.get("label"),
    "contracts": trade.get("contracts"),
    "cost_usd": trade.get("cost_usd"),
    "entry_price_cents": trade.get("entry_price_cents") or trade.get("price_cents"),
    "status": trade.get("status"),
    "signal": trade.get("signal"),
  }


def _exit_row(trade: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": trade.get("id"),
    "created_at": trade.get("created_at"),
    "side": trade.get("side"),
    "market_ticker": trade.get("market_ticker"),
    "label": trade.get("label"),
    "contracts": trade.get("contracts"),
    "exit_price_cents": trade.get("exit_price_cents") or trade.get("price_cents"),
    "pnl_usd": trade.get("realized_pnl_usd") if trade.get("realized_pnl_usd") is not None else trade.get("pnl_usd"),
    "exit_reason": _exit_reason(trade),
    "status": trade.get("status"),
    "detail": trade.get("detail"),
  }


def _hour_side(
  store: HourlyBotStore,
  event_ticker: str,
  *,
  mode: str,
) -> dict[str, Any]:
  trades = _filter_trades_by_mode(
    store.list_trades(limit=200, event_ticker=event_ticker),
    mode,
  )
  entries = [
    _entry_row(t)
    for t in trades
    if t.get("action") == "enter" and str(t.get("status") or "") in _FILLED_ENTER
  ]
  exits = [
    _exit_row(t)
    for t in trades
    if t.get("action") == "exit" and str(t.get("status") or "") in _REALIZED_EXIT
  ]
  entries.sort(key=lambda r: r.get("created_at") or "")
  exits.sort(key=lambda r: r.get("created_at") or "")
  summary = store.hour_interval_summary(event_ticker, mode=mode)
  realized = float(summary.get("realized_pnl_usd") or 0)
  return {
    "mode": mode,
    "summary": summary,
    "entries": entries,
    "exits": exits,
    "net_pnl_usd": round(realized, 2),
    "has_activity": bool(entries or exits),
  }


def build_hourly_live_trial_compare(
  live_store: HourlyBotStore,
  trial_store: HourlyBotStore,
  *,
  asset: str,
  limit_hours: int = 24,
  live_mode: str = "live",
  trial_mode: str | None = None,
) -> dict[str, Any]:
  """Compare live hourly bot vs hourly_trial for matched event_tickers."""
  if trial_mode is None:
    trial_mode = trial_store.get_settings().mode or "paper"

  live_events = _recent_event_tickers(live_store, mode=live_mode, limit=limit_hours * 2)
  trial_events = _recent_event_tickers(trial_store, mode=trial_mode, limit=limit_hours * 2)

  last_by_event: dict[str, datetime] = {}
  for evt, ts in live_events + trial_events:
    prev = last_by_event.get(evt)
    if prev is None or ts > prev:
      last_by_event[evt] = ts

  # Prefer hours where both bots traded; fill with recent union if needed.
  live_set = {evt for evt, _ in live_events}
  trial_set = {evt for evt, _ in trial_events}
  matched = [evt for evt in last_by_event if evt in live_set and evt in trial_set]
  matched.sort(key=lambda e: last_by_event[e], reverse=True)

  event_tickers = matched[:limit_hours]
  if len(event_tickers) < limit_hours:
    extras = [
      evt
      for evt in sorted(last_by_event, key=lambda e: last_by_event[e], reverse=True)
      if evt not in event_tickers
    ]
    event_tickers.extend(extras[: max(0, limit_hours - len(event_tickers))])

  hours: list[dict[str, Any]] = []
  for event_ticker in event_tickers:
    live = _hour_side(live_store, event_ticker, mode=live_mode)
    trial = _hour_side(trial_store, event_ticker, mode=trial_mode)
    hours.append(
      {
        "event_ticker": event_ticker,
        "last_activity_at": last_by_event.get(event_ticker, datetime.now(timezone.utc)).isoformat(),
        "both_active": live["has_activity"] and trial["has_activity"],
        "live": live,
        "trial": trial,
        "pnl_delta_usd": round(live["net_pnl_usd"] - trial["net_pnl_usd"], 2),
      }
    )

  return {
    "ok": True,
    "asset": asset,
    "live_kind": "hourly",
    "trial_kind": "hourly_trial",
    "live_mode": live_mode,
    "trial_mode": trial_mode,
    "limit_hours": limit_hours,
    "matched_event_count": len(matched),
    "hours": hours,
    "generated_at": datetime.now(timezone.utc).isoformat(),
  }
