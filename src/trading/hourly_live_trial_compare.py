"""Side-by-side comparison of live hourly bot vs paper hourly trial per event."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.slot15_bot_store import Slot15BotStore
from src.trading.hourly_event_time import canonical_hourly_event_ticker

BotStore = HourlyBotStore | Slot15BotStore

_FILLED_ENTER = ("filled", "reconciled")
_PENDING_RESTING = ("resting",)
_REALIZED_EXIT = ("filled", "reconciled")
_EXIT_REASON_RE = re.compile(r"EXIT\s*\(([^)]+)\)", re.IGNORECASE)


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _entry_key(entry: dict[str, Any]) -> tuple[str, str]:
  return (
    str(entry.get("market_ticker") or "").upper(),
    str(entry.get("side") or "").lower(),
  )


def pair_entries_across_bots(
  live_entries: list[dict[str, Any]],
  trial_entries: list[dict[str, Any]],
  *,
  window_seconds: int = 180,
) -> dict[str, Any]:
  """Match entries on market_ticker + side within a time window."""
  used_trial: set[int] = set()
  pairs: list[dict[str, Any]] = []
  unpaired_live: list[dict[str, Any]] = []

  for live in live_entries:
    live_ts = _parse_ts(live.get("created_at"))
    live_key = _entry_key(live)
    best_idx: int | None = None
    best_delta: float | None = None
    for idx, trial in enumerate(trial_entries):
      if idx in used_trial:
        continue
      if _entry_key(trial) != live_key:
        continue
      trial_ts = _parse_ts(trial.get("created_at"))
      if live_ts is None or trial_ts is None:
        if best_idx is None:
          best_idx = idx
          best_delta = None
        continue
      delta = abs((live_ts - trial_ts).total_seconds())
      if delta > window_seconds:
        continue
      if best_delta is None or delta < best_delta:
        best_idx = idx
        best_delta = delta
    if best_idx is None:
      unpaired_live.append(live)
      continue
    used_trial.add(best_idx)
    trial = trial_entries[best_idx]
    live_px = live.get("entry_price_cents")
    trial_px = trial.get("entry_price_cents")
    px_delta = None
    if live_px is not None and trial_px is not None:
      px_delta = int(live_px) - int(trial_px)
    pairs.append(
      {
        "market_ticker": live.get("market_ticker"),
        "side": live.get("side"),
        "label": live.get("label") or trial.get("label"),
        "live": live,
        "trial": trial,
        "time_delta_seconds": round(best_delta, 1) if best_delta is not None else None,
        "entry_price_delta_cents": px_delta,
      }
    )

  unpaired_trial = [e for i, e in enumerate(trial_entries) if i not in used_trial]
  deltas = [p["entry_price_delta_cents"] for p in pairs if p["entry_price_delta_cents"] is not None]
  avg_px_delta = round(sum(deltas) / len(deltas), 1) if deltas else None
  return {
    "pairs": pairs,
    "unpaired_live": unpaired_live,
    "unpaired_trial": unpaired_trial,
    "paired_count": len(pairs),
    "avg_entry_price_delta_cents": avg_px_delta,
    "pair_window_seconds": window_seconds,
  }


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
  store: BotStore,
  *,
  mode: str,
  limit: int,
  stats_epoch_at: str | None = None,
) -> list[tuple[str, datetime]]:
  from src.trading.bot_runtime import event_in_stats_epoch

  with store._connect() as conn:
    rows = conn.execute(
      f"""
      SELECT event_ticker, MAX(created_at) AS last_at, MIN(created_at) AS first_at
      FROM bot_trades
      WHERE event_ticker IS NOT NULL AND TRIM(event_ticker) != ''
        AND mode = ?
      GROUP BY event_ticker
      ORDER BY last_at DESC
      LIMIT ?
      """,
      (mode, limit * 4),
    ).fetchall()
  out: list[tuple[str, datetime]] = []
  for row in rows:
    evt = str(row["event_ticker"])
    if not event_in_stats_epoch(evt, stats_epoch_at, first_trade_at=row["first_at"]):
      continue
    ts = _parse_ts(row["last_at"])
    if ts is not None:
      out.append((evt, ts))
    if len(out) >= limit:
      break
  return out


def _canonicalize_event_list(
  events: list[tuple[str, datetime]],
) -> list[tuple[str, datetime]]:
  """Merge threshold/range sibling tickers (KXBTCD vs KXBTC) into one hour row."""
  merged: dict[str, datetime] = {}
  for evt, ts in events:
    canon = canonical_hourly_event_ticker(evt)
    prev = merged.get(canon)
    if prev is None or ts > prev:
      merged[canon] = ts
  return sorted(merged.items(), key=lambda item: item[1], reverse=True)


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
  from src.trading.bot_exit_pnl import effective_exit_pnl_usd

  pnl = effective_exit_pnl_usd(trade)
  return {
    "id": trade.get("id"),
    "created_at": trade.get("created_at"),
    "side": trade.get("side"),
    "market_ticker": trade.get("market_ticker"),
    "label": trade.get("label"),
    "contracts": trade.get("contracts"),
    "exit_price_cents": trade.get("exit_price_cents") or trade.get("price_cents"),
    "pnl_usd": pnl,
    "exit_reason": _exit_reason(trade),
    "status": trade.get("status"),
    "detail": trade.get("detail"),
  }


def _interval_summary(store: BotStore, event_ticker: str, *, mode: str) -> dict[str, Any]:
  slot_fn = getattr(store, "slot_interval_summary", None)
  if callable(slot_fn):
    return slot_fn(event_ticker, mode=mode)
  return store.hour_interval_summary(event_ticker, mode=mode)


def _period_side(
  store: BotStore,
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
  pending_resting = [
    _entry_row(t)
    for t in trades
    if t.get("action") == "enter" and str(t.get("status") or "") in _PENDING_RESTING
  ]
  exits = [
    _exit_row(t)
    for t in trades
    if t.get("action") == "exit" and str(t.get("status") or "") in _REALIZED_EXIT
  ]
  entries.sort(key=lambda r: r.get("created_at") or "")
  exits.sort(key=lambda r: r.get("created_at") or "")
  summary = _interval_summary(store, event_ticker, mode=mode)
  realized = float(summary.get("realized_pnl_usd") or 0)
  return {
    "mode": mode,
    "summary": summary,
    "entries": entries,
    "pending_resting": pending_resting,
    "exits": exits,
    "net_pnl_usd": round(realized, 2),
    "has_activity": bool(entries or exits),
    "has_pending_resting": bool(pending_resting),
  }


def build_hourly_live_trial_compare(
  live_store: BotStore,
  trial_store: BotStore,
  *,
  asset: str,
  limit_hours: int = 24,
  live_mode: str = "live",
  trial_mode: str | None = None,
  trial_kind: str = "hourly_trial",
  live_kind: str = "hourly",
  pair_window_seconds: int = 180,
  stats_epoch_at: str | None = None,
) -> dict[str, Any]:
  """Compare live bot vs paper trial for matched event_tickers (hourly or 15m slot)."""
  if trial_mode is None:
    trial_mode = trial_store.get_settings().mode or "paper"

  if stats_epoch_at is None:
    from src.trading.bot_runtime import stats_epoch_at as read_stats_epoch_at

    with live_store._connect() as conn:
      stats_epoch_at = read_stats_epoch_at(conn)

  live_events = _canonicalize_event_list(_recent_event_tickers(
    live_store,
    mode=live_mode,
    limit=limit_hours * 2,
    stats_epoch_at=stats_epoch_at,
  ))
  trial_events = _canonicalize_event_list(_recent_event_tickers(
    trial_store,
    mode=trial_mode,
    limit=limit_hours * 2,
    stats_epoch_at=stats_epoch_at,
  ))

  last_by_event: dict[str, datetime] = {}
  for evt, ts in live_events + trial_events:
    prev = last_by_event.get(evt)
    if prev is None or ts > prev:
      last_by_event[evt] = ts

  # Prefer hours where both bots traded; fill with recent union if needed.
  live_set = {evt for evt, _ in live_events}
  trial_set = {evt for evt, _ in trial_events}
  matched = [evt for evt in last_by_event if evt in live_set and evt in trial_set]

  from src.trading.bot_runtime import event_in_stats_epoch

  if stats_epoch_at:
    matched = [evt for evt in matched if event_in_stats_epoch(evt, stats_epoch_at)]
    last_by_event = {
      evt: ts
      for evt, ts in last_by_event.items()
      if event_in_stats_epoch(evt, stats_epoch_at)
    }

  matched.sort(key=lambda e: last_by_event[e], reverse=True)

  event_tickers = [
    evt for evt in matched[:limit_hours]
    if event_in_stats_epoch(evt, stats_epoch_at)
  ]
  if len(event_tickers) < limit_hours:
    extras = [
      evt
      for evt in sorted(last_by_event, key=lambda e: last_by_event[e], reverse=True)
      if evt not in event_tickers and event_in_stats_epoch(evt, stats_epoch_at)
    ]
    event_tickers.extend(extras[: max(0, limit_hours - len(event_tickers))])

  hours: list[dict[str, Any]] = []
  for event_ticker in event_tickers:
    live = _period_side(live_store, event_ticker, mode=live_mode)
    trial = _period_side(trial_store, event_ticker, mode=trial_mode)
    entry_pairs = pair_entries_across_bots(
      live["entries"],
      trial["entries"],
      window_seconds=pair_window_seconds,
    )
    hours.append(
      {
        "event_ticker": event_ticker,
        "last_activity_at": last_by_event.get(event_ticker, datetime.now(timezone.utc)).isoformat(),
        "both_active": live["has_activity"] and trial["has_activity"],
        "live": live,
        "trial": trial,
        "entry_pairs": entry_pairs,
        "pnl_delta_usd": round(live["net_pnl_usd"] - trial["net_pnl_usd"], 2),
      }
    )

  return {
    "ok": True,
    "asset": asset,
    "live_kind": live_kind,
    "trial_kind": trial_kind,
    "live_mode": live_mode,
    "trial_mode": trial_mode,
    "limit_hours": limit_hours,
    "pair_window_seconds": pair_window_seconds,
    "stats_epoch_at": stats_epoch_at,
    "matched_event_count": len(matched),
    "hours": hours,
    "generated_at": datetime.now(timezone.utc).isoformat(),
  }


def build_slot15_live_trial_compare(
  live_store: Slot15BotStore,
  trial_store: Slot15BotStore,
  *,
  asset: str,
  limit_slots: int = 48,
  pair_window_seconds: int = 180,
) -> dict[str, Any]:
  """Compare main 15m bot vs paper 15m trial per slot_key.

  Uses the main bot's current settings mode (paper or live) so paper-only
  trials still populate the compare panel before switching live on.
  """
  main_mode = str(live_store.get_settings().mode or "live").lower()
  if main_mode not in ("live", "paper"):
    main_mode = "live"
  return build_hourly_live_trial_compare(
    live_store,
    trial_store,
    asset=asset,
    limit_hours=limit_slots,
    live_mode=main_mode,
    trial_kind="slot15_trial",
    live_kind="slot15",
    pair_window_seconds=pair_window_seconds,
  )
