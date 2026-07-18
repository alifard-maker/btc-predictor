"""Reconcile manual (human) live legs with Kalshi fills.

When the user sells on Kalshi (outside the dashboard), human positions can stay
open and later get wrongly hour-settled. This module:

1) Closes open live legs from Kalshi sell fills when inventory is flat.
2) Rebuilds a finished event's live P&L from Kalshi fill history (paper untouched).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from typing import Any

from src.trading.hourly_event_time import (
  canonical_hourly_event_ticker,
  hourly_event_settle_utc,
  market_ticker_event_ticker,
)
from src.trading.paper_execution import leg_pnl_usd

log = logging.getLogger(__name__)

_LABEL_BY_FLOOR = {
  63999.99: "$64,000 or above",
  64099.99: "$64,100 or above",
  64199.99: "$64,200 or above",
}


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


def _yes_dollars(fill: dict[str, Any]) -> float | None:
  raw = fill.get("yes_price_dollars")
  if raw is None or raw == "":
    return None
  try:
    return float(raw)
  except (TypeError, ValueError):
    return None


def _fee_dollars(fill: dict[str, Any]) -> float:
  try:
    return float(fill.get("fee_cost") or 0.0)
  except (TypeError, ValueError):
    return 0.0


def _ticker(fill: dict[str, Any]) -> str:
  return str(fill.get("ticker") or fill.get("market_ticker") or "").strip()


def _is_buy_yes(fill: dict[str, Any]) -> bool:
  action = str(fill.get("action") or "").lower()
  side = str(fill.get("side") or fill.get("outcome_side") or "").lower()
  return action == "buy" and side == "yes"


def _is_sell_yes_inventory(fill: dict[str, Any]) -> bool:
  """Kalshi often records selling YES as action=sell side=no with yes_price set."""
  action = str(fill.get("action") or "").lower()
  if action != "sell":
    return False
  return _yes_dollars(fill) is not None


def label_for_ticker(ticker: str, kalshi: Any | None = None) -> str:
  # Prefer known floor → label; fall back to market title / ticker tail.
  try:
    floor = float(str(ticker).rsplit("-T", 1)[-1])
  except (TypeError, ValueError, IndexError):
    floor = None
  if floor is not None and floor in _LABEL_BY_FLOOR:
    return _LABEL_BY_FLOOR[floor]
  if floor is not None and abs(floor - round(floor) + 0.01) < 1e-6:
    # e.g. 63999.99 → $64,000 or above
    return f"${round(floor + 0.01):,.0f} or above"
  if kalshi:
    try:
      row = kalshi.get_market_ticker(ticker)
      title = str((row or {}).get("title") or "").strip()
      if title:
        return title
    except Exception:
      pass
  return ticker.rsplit("-", 1)[-1]


def aggregate_yes_round_trips(
  fills: list[dict[str, Any]],
  *,
  event_ticker: str,
) -> dict[str, dict[str, Any]]:
  """Per-market YES buy/sell aggregates for one hourly event."""
  event = canonical_hourly_event_ticker(str(event_ticker))
  by: dict[str, dict[str, Any]] = {}
  for fill in fills:
    ticker = _ticker(fill)
    if not ticker:
      continue
    if canonical_hourly_event_ticker(market_ticker_event_ticker(ticker)) != event:
      continue
    yes = _yes_dollars(fill)
    ct = _fill_count(fill)
    if yes is None or ct <= 0:
      continue
    g = by.setdefault(
      ticker,
      {
        "market_ticker": ticker,
        "buy_contracts": 0.0,
        "buy_notional": 0.0,
        "buy_fees": 0.0,
        "sell_contracts": 0.0,
        "sell_notional": 0.0,
        "sell_fees": 0.0,
      },
    )
    fee = _fee_dollars(fill)
    if _is_buy_yes(fill):
      g["buy_contracts"] += ct
      g["buy_notional"] += ct * yes
      g["buy_fees"] += fee
    elif _is_sell_yes_inventory(fill):
      g["sell_contracts"] += ct
      g["sell_notional"] += ct * yes
      g["sell_fees"] += fee
  out: dict[str, dict[str, Any]] = {}
  for ticker, g in by.items():
    buy_ct = float(g["buy_contracts"])
    sell_ct = float(g["sell_contracts"])
    if buy_ct < 0.05:
      continue
    cost = float(g["buy_notional"])
    proceeds = float(g["sell_notional"])
    fees = float(g["buy_fees"]) + float(g["sell_fees"])
    # Unsold YES that expired worthless is already reflected: full buy cost, no proceeds.
    pnl = round(proceeds - cost - fees, 2)
    entry_cents = int(round(cost / buy_ct * 100)) if buy_ct else None
    if sell_ct >= 0.05:
      exit_cents = int(round(proceeds / sell_ct * 100))
    else:
      exit_cents = 0
    contracts = max(1, int(round(buy_ct)))
    out[ticker] = {
      "market_ticker": ticker,
      "side": "yes",
      "contracts": contracts,
      "buy_contracts": round(buy_ct, 4),
      "sell_contracts": round(sell_ct, 4),
      "entry_price_cents": entry_cents,
      "exit_price_cents": exit_cents,
      "cost_usd": round(cost, 2),
      "proceeds_usd": round(proceeds, 2),
      "fees_usd": round(fees, 2),
      "pnl_usd": pnl,
      "return_pct": round(pnl / cost * 100.0, 1) if cost > 0.009 else None,
    }
  return out


def _fetch_event_fills(kalshi: Any, event_ticker: str) -> list[dict[str, Any]]:
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return []
  event = canonical_hourly_event_ticker(str(event_ticker))
  fills = kalshi.list_fills(limit=1000) or []
  out = []
  for f in fills:
    if not isinstance(f, dict):
      continue
    t = _ticker(f)
    if not t:
      continue
    if canonical_hourly_event_ticker(market_ticker_event_ticker(t)) == event:
      out.append(f)
  return out


def _live_event_pnl(store: Any, event: str) -> float:
  total = 0.0
  for t in store.list_trades(limit=500, event_ticker=event):
    if str(t.get("mode") or "").lower() != "live" or t.get("action") != "exit":
      continue
    try:
      total += float(t.get("pnl_usd") or 0)
    except (TypeError, ValueError):
      pass
  return round(total, 2)


def sync_recent_human_live_hours_from_kalshi(
  store: Any,
  *,
  kalshi: Any,
  asset: str = "btc",
  lookback_fills: int = 1000,
) -> list[dict[str, Any]]:
  """
  Ensure every recent BTC/ETH hourly event with Kalshi fills appears in the
  manual live trade log (not only legs opened from the dashboard).
  """
  from datetime import datetime, timezone

  prefix = "KXBTCD-" if str(asset).lower() == "btc" else "KXETHD-"
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return []
  fills = kalshi.list_fills(limit=lookback_fills) or []
  events: set[str] = set()
  for f in fills:
    if not isinstance(f, dict):
      continue
    t = _ticker(f)
    if not t.startswith(prefix):
      continue
    events.add(canonical_hourly_event_ticker(market_ticker_event_ticker(t)))

  repaired: list[dict[str, Any]] = []
  now = datetime.now(timezone.utc)
  for event in sorted(events):
    if not event:
      continue
    event_fills = [
      f for f in fills
      if isinstance(f, dict)
      and canonical_hourly_event_ticker(market_ticker_event_ticker(_ticker(f))) == event
    ]
    legs = aggregate_yes_round_trips(event_fills, event_ticker=event)
    if not legs:
      continue
    settle_at = hourly_event_settle_utc(event)
    settled = settle_at is not None and now >= settle_at
    live_rows = [
      t for t in store.list_trades(limit=500, event_ticker=event)
      if str(t.get("mode") or "").lower() == "live"
    ]
    live_exits = [t for t in live_rows if t.get("action") == "exit"]
    live_enters = [t for t in live_rows if t.get("action") == "enter"]
    open_live = [
      p for p in store.open_positions(event)
      if str(p.get("mode") or "").lower() == "live"
    ]
    has_any_live = bool(live_rows or open_live)
    # Manual Kalshi-history pins (mixed YES/NO books) — do not overwrite.
    pinned = any(
      "matched Kalshi history" in str(t.get("detail") or "")
      for t in live_exits
    )
    if pinned:
      continue
    # Unsettled hour with leftover inventory: only import if the dashboard
    # has nothing yet (don't churn-rebuild open MTM every poll).
    if not settled:
      if has_any_live:
        continue
      out = rebuild_human_live_event_from_kalshi(store, kalshi=kalshi, event_ticker=event)
      if out.get("ok"):
        repaired.append(out)
      continue

    kalshi_pnl = round(sum(float(g["pnl_usd"]) for g in legs.values()), 2)
    ledger_pnl = _live_event_pnl(store, event)
    missing = not has_any_live
    stale = any(
      "HOUR SETTLEMENT" in str(t.get("detail") or "")
      or "SLOT SETTLEMENT" in str(t.get("detail") or "")
      for t in live_exits
    )
    mismatched = abs(ledger_pnl - kalshi_pnl) > 0.50
    ledger_tickers = {
      str(t.get("market_ticker") or "")
      for t in live_exits + live_enters + open_live
    }
    incomplete = any(ticker not in ledger_tickers for ticker in legs)
    if not (missing or stale or mismatched or incomplete):
      continue
    out = rebuild_human_live_event_from_kalshi(store, kalshi=kalshi, event_ticker=event)
    if out.get("ok"):
      repaired.append(out)
  return repaired


def rebuild_human_live_event_from_kalshi(
  store: Any,
  *,
  kalshi: Any,
  event_ticker: str,
  asset: str = "btc",
) -> dict[str, Any]:
  """
  Replace live enter/exit rows for *event_ticker* with Kalshi-fill accounting.

  Paper rows are left alone. Used when the user traded (or sized up / sold) on Kalshi.
  Unsold inventory on an unsettled hour stays open.
  """
  del asset  # reserved
  from datetime import datetime, timezone

  event = canonical_hourly_event_ticker(str(event_ticker))
  fills = _fetch_event_fills(kalshi, event)
  legs = aggregate_yes_round_trips(fills, event_ticker=event)
  if not legs:
    return {"ok": False, "error": "no_kalshi_fills", "event_ticker": event}

  settle_at = hourly_event_settle_utc(event)
  settled = settle_at is not None and datetime.now(timezone.utc) >= settle_at

  removed = store.purge_mode_trades_for_event(event, mode="live")
  for pos in list(store.open_positions(event)):
    if str(pos.get("mode") or "").lower() == "live":
      store.close_position(str(pos["id"]))

  written: list[dict[str, Any]] = []
  total_pnl = 0.0
  total_cost = 0.0
  for ticker, leg in sorted(legs.items()):
    label = label_for_ticker(ticker, kalshi)
    buy_ct = float(leg["buy_contracts"])
    sell_ct = float(leg["sell_contracts"])
    leftover = buy_ct - sell_ct
    entry_c = int(leg["entry_price_cents"] or 0)
    fees = float(leg["fees_usd"])
    cost = float(leg["cost_usd"])

    # Still carrying size on an unsettled hour → keep open (no fake settle).
    if leftover >= 0.05 and not settled:
      open_cost = round(cost * (leftover / buy_ct), 2) if buy_ct else round(cost, 2)
      open_ct = max(1, int(round(leftover)))
      pid = str(uuid.uuid4())
      store.open_position({
        "id": pid,
        "event_ticker": event,
        "market_ticker": ticker,
        "side": "yes",
        "contracts": open_ct,
        "entry_price_cents": entry_c,
        "cost_usd": open_cost,
        "signal": "KALSHI_SYNC",
        "label": label,
        "mode": "live",
      })
      store.log_trade({
        "event_ticker": event,
        "action": "enter",
        "mode": "live",
        "market_ticker": ticker,
        "side": "yes",
        "contracts": open_ct,
        "price_cents": entry_c,
        "entry_price_cents": entry_c,
        "cost_usd": open_cost,
        "signal": "KALSHI_SYNC",
        "label": label,
        "status": "filled",
        "detail": f"Kalshi sync LIVE enter YES@{entry_c}¢ · {leftover:.2f} ct still open",
        "position_id": pid,
        "entry_context": {"source": "kalshi_fill_rebuild", "leg": leg, "open": True},
      })
      total_cost += open_cost
      written.append({**leg, "label": label, "position_id": pid, "open": True})
      # Also record sold portion if any
      if sell_ct < 0.05:
        continue
      sold_cost = round(cost - open_cost, 2)
      proceeds = float(leg["proceeds_usd"])
      # Approximate closed fees as remainder
      closed_fees = max(0.0, fees * (sell_ct / buy_ct)) if buy_ct else fees
      pnl = round(proceeds - sold_cost - closed_fees, 2)
      exit_c = int(leg["exit_price_cents"] or 0)
      pid2 = str(uuid.uuid4())
      contracts = max(1, int(round(sell_ct)))
      store.open_position({
        "id": pid2,
        "event_ticker": event,
        "market_ticker": ticker,
        "side": "yes",
        "contracts": contracts,
        "entry_price_cents": entry_c,
        "cost_usd": sold_cost,
        "signal": "KALSHI_SYNC",
        "label": label,
        "mode": "live",
      })
      store.close_position(pid2)
      store.log_trade({
        "event_ticker": event,
        "action": "enter",
        "mode": "live",
        "market_ticker": ticker,
        "side": "yes",
        "contracts": contracts,
        "price_cents": entry_c,
        "entry_price_cents": entry_c,
        "cost_usd": sold_cost,
        "signal": "KALSHI_SYNC",
        "label": label,
        "status": "filled",
        "detail": f"Kalshi sync LIVE enter YES@{entry_c}¢ · {sell_ct:.2f} ct (sold portion)",
        "position_id": pid2,
      })
      store.log_trade({
        "event_ticker": event,
        "action": "exit",
        "mode": "live",
        "market_ticker": ticker,
        "side": "yes",
        "contracts": contracts,
        "price_cents": exit_c,
        "entry_price_cents": entry_c,
        "exit_price_cents": exit_c,
        "cost_usd": sold_cost,
        "pnl_usd": pnl,
        "signal": "KALSHI_SYNC",
        "label": label,
        "status": "filled",
        "detail": f"Kalshi sync LIVE exit @ {exit_c}¢ · P&L {pnl:+.2f} — sold on Kalshi",
        "position_id": pid2,
      })
      total_pnl += pnl
      total_cost += sold_cost
      continue

    # Flat or settled leftover → one closed round-trip (leftover after settle @ 0¢).
    exit_c = int(leg["exit_price_cents"] or 0)
    if leftover >= 0.05 and settled and sell_ct >= 0.05:
      # Blended exit: sold proceeds / full buy size
      exit_c = int(round(float(leg["proceeds_usd"]) / buy_ct * 100))
    elif leftover >= 0.05 and settled and sell_ct < 0.05:
      exit_c = 0
    contracts = int(leg["contracts"])
    pnl = float(leg["pnl_usd"])
    pid = str(uuid.uuid4())
    total_pnl += pnl
    total_cost += cost
    store.open_position({
      "id": pid,
      "event_ticker": event,
      "market_ticker": ticker,
      "side": "yes",
      "contracts": contracts,
      "entry_price_cents": entry_c,
      "cost_usd": cost,
      "signal": "KALSHI_SYNC",
      "label": label,
      "mode": "live",
    })
    store.close_position(pid)
    store.log_trade({
      "event_ticker": event,
      "action": "enter",
      "mode": "live",
      "market_ticker": ticker,
      "side": "yes",
      "contracts": contracts,
      "price_cents": entry_c,
      "entry_price_cents": entry_c,
      "cost_usd": cost,
      "signal": "KALSHI_SYNC",
      "label": label,
      "status": "filled",
      "detail": (
        f"Kalshi sync LIVE enter YES@{entry_c}¢ · "
        f"{leg['buy_contracts']} ct · fees ${leg['fees_usd']:.2f}"
      ),
      "position_id": pid,
      "entry_context": {"source": "kalshi_fill_rebuild", "leg": leg},
    })
    note = (
      "sold on Kalshi (not hour settlement)"
      if sell_ct + 0.05 >= buy_ct
      else "Kalshi fills + settlement on leftover"
    )
    store.log_trade({
      "event_ticker": event,
      "action": "exit",
      "mode": "live",
      "market_ticker": ticker,
      "side": "yes",
      "contracts": contracts,
      "price_cents": exit_c,
      "entry_price_cents": entry_c,
      "exit_price_cents": exit_c,
      "cost_usd": cost,
      "pnl_usd": pnl,
      "signal": "KALSHI_SYNC",
      "label": label,
      "status": "filled",
      "detail": (
        f"Kalshi sync LIVE exit @ {exit_c}¢ · P&L {pnl:+.2f} "
        f"({leg['return_pct']:+.0f}% after ${leg['fees_usd']:.2f} fees) — {note}"
      ),
      "position_id": pid,
      "entry_context": {
        "source": "kalshi_fill_rebuild",
        "exit_reason": "kalshi_sell_sync",
        "leg": leg,
        "return_pct": leg["return_pct"],
      },
    })
    written.append({**leg, "label": label, "position_id": pid})

  log.info(
    "Rebuilt human live event %s from Kalshi: %d legs, cost=$%.2f pnl=%+.2f (removed %d old live rows)",
    event,
    len(written),
    total_cost,
    total_pnl,
    removed,
  )
  return {
    "ok": True,
    "event_ticker": event,
    "removed_live_rows": removed,
    "legs": written,
    "cost_usd": round(total_cost, 2),
    "pnl_usd": round(total_pnl, 2),
    "return_pct": round(total_pnl / total_cost * 100.0, 1) if total_cost > 0.009 else None,
  }


def sync_open_human_live_exits_from_kalshi(
  store: Any,
  *,
  kalshi: Any,
  asset: str = "btc",
) -> list[dict[str, Any]]:
  """
  If an open live human leg is flat (or reduced) on Kalshi via sells, close it
  at the fill-implied exit so hour settlement cannot invent a 100¢/0¢ result.
  """
  del asset
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return []
  open_live = [
    p for p in store.open_positions()
    if str(p.get("mode") or "").lower() == "live"
  ]
  if not open_live:
    return []

  # Group open live legs by event and rebuild that event when sells exist.
  by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
  for pos in open_live:
    ev = canonical_hourly_event_ticker(str(pos.get("event_ticker") or ""))
    if ev:
      by_event[ev].append(pos)

  closed: list[dict[str, Any]] = []
  for event, positions in by_event.items():
    fills = _fetch_event_fills(kalshi, event)
    legs = aggregate_yes_round_trips(fills, event_ticker=event)
    for pos in positions:
      ticker = str(pos.get("market_ticker") or "")
      leg = legs.get(ticker)
      if not leg:
        continue
      # Close only when sells cover most of the bought size (flat-ish).
      if float(leg["sell_contracts"]) + 0.05 < float(leg["buy_contracts"]) * 0.85:
        # Still carrying size — leave open for later / settlement.
        continue
      # Rebuild whole event once sells are material (handles size-ups on Kalshi).
      out = rebuild_human_live_event_from_kalshi(
        store, kalshi=kalshi, event_ticker=event,
      )
      if out.get("ok"):
        closed.extend(out.get("legs") or [])
      break
  return closed


def repair_stale_human_live_settlements_from_kalshi(
  store: Any,
  *,
  kalshi: Any,
  event_tickers: list[str] | None = None,
  asset: str = "btc",
) -> list[dict[str, Any]]:
  """
  Find live exits marked as HOUR SETTLEMENT (or mismatched vs Kalshi) and rebuild
  those events from Kalshi fills when sell fills prove an early exit / size-up.
  """
  del asset
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return []
  trades = store.list_trades(limit=2000)
  events: set[str] = set()
  if event_tickers:
    events = {canonical_hourly_event_ticker(e) for e in event_tickers}
  else:
    for t in trades:
      if str(t.get("mode") or "").lower() != "live" or t.get("action") != "exit":
        continue
      detail = str(t.get("detail") or "")
      if "HOUR SETTLEMENT" in detail or "SLOT SETTLEMENT" in detail:
        events.add(canonical_hourly_event_ticker(str(t.get("event_ticker") or "")))

  repaired: list[dict[str, Any]] = []
  for event in sorted(events):
    if not event:
      continue
    fills = _fetch_event_fills(kalshi, event)
    legs = aggregate_yes_round_trips(fills, event_ticker=event)
    if not legs:
      continue
    has_sells = any(float(g["sell_contracts"]) > 0.05 for g in legs.values())
    if not has_sells:
      continue
    live_exits = [
      t for t in store.list_trades(limit=500, event_ticker=event)
      if str(t.get("mode") or "").lower() == "live" and t.get("action") == "exit"
    ]
    stale = any(
      "HOUR SETTLEMENT" in str(t.get("detail") or "")
      or "SLOT SETTLEMENT" in str(t.get("detail") or "")
      for t in live_exits
    )
    ledger_pnl = round(sum(float(t.get("pnl_usd") or 0) for t in live_exits), 2)
    kalshi_pnl = round(sum(float(g["pnl_usd"]) for g in legs.values()), 2)
    if not stale and abs(ledger_pnl - kalshi_pnl) <= 0.05:
      continue
    out = rebuild_human_live_event_from_kalshi(store, kalshi=kalshi, event_ticker=event)
    if out.get("ok"):
      repaired.append(out)
  return repaired
