"""Kalshi portfolio realized P&L by category (fills + settlements)."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.data.kalshi import KalshiClient
from src.trading.hourly_event_time import market_ticker_event_ticker
from src.trading.kalshi_fill_sync import (
  _aggregate_fills_to_orders,
  _build_order_direction_cache,
  _settlement_created_at,
  pair_fifo_closed_legs,
)
from src.trading.kalshi_portfolio_pnl_store import (
  KalshiPortfolioPnlStore,
  portfolio_pnl_db_path,
)
from src.trading.paper_execution import leg_pnl_usd

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_REPORT_CACHE: dict[str, Any] = {"mono_at": 0.0, "payload": None, "cfg_key": None}
_REPORT_CACHE_TTL_SEC = 110.0

PNL_SOURCE_KALSHI_WALLET = "kalshi_wallet"
PNL_SOURCE_BOT_LOG = "bot_log"
PNL_SOURCE_KALSHI_FILLS = "kalshi_fills"
TARGET_WEEKLY_USD = 4000.0


def categorize_ticker(ticker: str) -> str:
  t = (ticker or "").upper()
  event = market_ticker_event_ticker(ticker).upper()
  base = event or t
  if "KXBTC15M" in base or base.startswith("KXBTC15M"):
    return "BTC 15m"
  if "KXETH15M" in base or base.startswith("KXETH15M"):
    return "ETH 15m"
  if base.startswith("KXBTCD") or base.startswith("KXBTC-"):
    return "BTC hourly"
  if base.startswith("KXETH"):
    return "ETH hourly"
  if base.startswith("KXINX") or "INX" in base or base.startswith("KXSPX"):
    return "SPX hourly"
  if base.startswith("KXNDX") or "NDX" in base:
    return "NDX hourly"
  if "FIFA" in base or base.startswith("KXFIFA"):
    return "FIFA / soccer"
  if "MLB" in base or base.startswith("KXMLB"):
    return "MLB sports"
  if "NBA" in base or base.startswith("KXNBA"):
    return "NBA sports"
  if "ATP" in base or base.startswith("KXATP"):
    return "Tennis (ATP)"
  if "WTA" in base or base.startswith("KXWTA"):
    return "Tennis (WTA)"
  if "NFL" in base or base.startswith("KXNFL"):
    return "NFL sports"
  if "NHL" in base or base.startswith("KXNHL"):
    return "NHL sports"
  if "KXWCGAME" in base or "WCGAME" in base:
    return "Other sports (WC tie)"
  if "GAME" in base or "MATCH" in base:
    return "Other sports"
  return "Other"


def kalshi_portfolio_pnl_store(cfg: dict[str, Any] | None) -> KalshiPortfolioPnlStore:
  return KalshiPortfolioPnlStore(portfolio_pnl_db_path(cfg))


def _to_utc(dt: datetime) -> datetime:
  if dt.tzinfo is None:
    return dt.replace(tzinfo=timezone.utc)
  return dt.astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
  return _to_utc(dt).isoformat()


def parse_stats_epoch(epoch_iso: str | None) -> datetime | None:
  if not epoch_iso:
    return None
  try:
    dt = datetime.fromisoformat(str(epoch_iso).replace("Z", "+00:00"))
  except ValueError:
    return None
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
  return _to_utc(dt)


def _at_or_after_epoch(ts: datetime, epoch: datetime | None) -> bool:
  if epoch is None:
    return True
  return _to_utc(ts) >= epoch


def day_window_et(now_et: datetime | None = None) -> tuple[datetime, datetime, str]:
  """Calendar day in ET: 00:01:00 through 23:59:59."""
  now = now_et or datetime.now(ET)
  day = now.date()
  start = datetime(day.year, day.month, day.day, 0, 1, 0, tzinfo=ET)
  end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=ET)
  label = start.strftime("%a %b %-d, %Y")
  return start, end, label


def day_window_for_date(day: date) -> tuple[datetime, datetime, str]:
  start = datetime(day.year, day.month, day.day, 0, 1, 0, tzinfo=ET)
  end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=ET)
  label = start.strftime("%a %b %-d, %Y")
  return start, end, label


def week_window_et(now_et: datetime | None = None) -> tuple[datetime, datetime, str]:
  """Sunday 00:01 through Saturday 23:59:59 ET."""
  now = now_et or datetime.now(ET)
  days_since_sunday = (now.weekday() + 1) % 7
  sunday = (now - timedelta(days=days_since_sunday)).date()
  saturday = sunday + timedelta(days=6)
  start = datetime(sunday.year, sunday.month, sunday.day, 0, 1, 0, tzinfo=ET)
  end = datetime(saturday.year, saturday.month, saturday.day, 23, 59, 59, tzinfo=ET)
  label = f"Sun {start.strftime('%b %-d')} – Sat {end.strftime('%b %-d, %Y')} (ET)"
  return start, end, label


def week_window_for_sunday(sunday: date) -> tuple[datetime, datetime, str]:
  saturday = sunday + timedelta(days=6)
  start = datetime(sunday.year, sunday.month, sunday.day, 0, 1, 0, tzinfo=ET)
  end = datetime(saturday.year, saturday.month, saturday.day, 23, 59, 59, tzinfo=ET)
  label = f"Sun {start.strftime('%b %-d')} – Sat {end.strftime('%b %-d, %Y')} (ET)"
  return start, end, label


def _in_window_et(ts: datetime, start_et: datetime, end_et: datetime) -> bool:
  ts_et = _to_utc(ts).astimezone(ET)
  return start_et <= ts_et <= end_et


def _window_hours(start_et: datetime, end_et: datetime, *, now_et: datetime | None = None) -> float:
  effective_end = end_et
  if now_et is not None:
    effective_end = min(end_et, now_et)
  secs = max((effective_end - start_et).total_seconds(), 60.0)
  return secs / 3600.0


def _leg_fingerprint(ticker: str, side: str, buy_at: datetime, exit_at: datetime, contracts: int) -> str:
  raw = f"{ticker}|{side}|{_iso_utc(buy_at)}|{_iso_utc(exit_at)}|{contracts}"
  return hashlib.sha1(raw.encode()).hexdigest()


def _entry_fingerprint(order_id: str, ticker: str, side: str, bought_at: datetime, contracts: int) -> str:
  raw = f"buy|{order_id}|{ticker}|{side}|{_iso_utc(bought_at)}|{contracts}"
  return hashlib.sha1(raw.encode()).hexdigest()


def _settlement_net_pnl_usd(row: dict[str, Any]) -> float | None:
  """Per-market net realized P&L from a Kalshi /portfolio/settlements row (matches Kalshi UI)."""
  result = str(row.get("market_result") or "").lower()
  if result in ("void", "scalar", ""):
    return None
  try:
    value = int(row.get("value") if row.get("value") not in (None, "") else 0)
    yes_count = float(row.get("yes_count_fp") or row.get("yes_count") or 0)
    no_count = float(row.get("no_count_fp") or row.get("no_count") or 0)
    yes_cost = float(row.get("yes_total_cost_dollars") or 0)
    no_cost = float(row.get("no_total_cost_dollars") or 0)
    fee = float(row.get("fee_cost") or 0)
  except (TypeError, ValueError):
    return None
  if yes_count < 0.05 and no_count < 0.05:
    return None
  yes_payout = yes_count * (value / 100.0)
  no_payout = no_count * ((100 - value) / 100.0)
  return round(yes_payout + no_payout - yes_cost - no_cost - fee, 2)


def _settlement_entry_cost_usd(row: dict[str, Any]) -> float:
  try:
    yes_cost = float(row.get("yes_total_cost_dollars") or 0)
    no_cost = float(row.get("no_total_cost_dollars") or 0)
  except (TypeError, ValueError):
    return 0.0
  return round(yes_cost + no_cost, 2)


def wallet_runway_kpi(
  week_block: dict[str, Any],
  *,
  target_usd: float = TARGET_WEEKLY_USD,
) -> dict[str, Any]:
  """Progress vs $4k/week target from a current_week summarize block."""
  pnl = round(float(week_block.get("total_pnl_usd") or 0), 2)
  legs = int(week_block.get("closed_legs") or 0)
  gap = round(float(target_usd) - pnl, 2)
  progress = round(100.0 * pnl / float(target_usd), 1) if target_usd else None
  return {
    "ok": True,
    "pnl_source": PNL_SOURCE_KALSHI_WALLET,
    "target_weekly_usd": float(target_usd),
    "week_pnl_usd": pnl,
    "closed_legs": legs,
    "pnl_per_hour_usd": week_block.get("pnl_per_hour_usd"),
    "pnl_per_leg_usd": week_block.get("pnl_per_leg_usd"),
    "win_rate": week_block.get("win_rate"),
    "gap_usd": gap,
    "progress_pct": progress,
    "on_pace": pnl >= float(target_usd) if legs > 0 else None,
    "label": week_block.get("label"),
    "window_start_et": week_block.get("window_start_et"),
    "window_end_et": week_block.get("window_end_et"),
  }


def closed_legs_since_epoch(
  kalshi: KalshiClient,
  since: datetime,
  *,
  asset: str | None = None,
  fill_limit: int = 2000,
) -> list[dict[str, Any]]:
  """Closed wallet legs with exit on/after since (optional hourly asset filter)."""
  from src.trading.hourly_event_time import hourly_fill_belongs_to_asset

  activity = portfolio_activity_from_kalshi(kalshi, fill_limit=fill_limit)
  since_u = _to_utc(since)
  out: list[dict[str, Any]] = []
  for leg in activity["closed"]:
    exit_at = leg.get("exit_at")
    if exit_at is None:
      continue
    if _to_utc(exit_at) < since_u:
      continue
    ticker = str(leg.get("ticker") or "")
    if asset and not hourly_fill_belongs_to_asset(ticker, asset):
      continue
    out.append(leg)
  return out


def kalshi_hourly_pnl_by_event_since(
  kalshi: KalshiClient,
  since: datetime,
  *,
  asset: str,
  fill_limit: int = 2000,
) -> dict[str, Any]:
  """Per hourly event Kalshi wallet P&L since an instant (shared with epoch reconcile)."""
  from src.trading.hourly_event_time import canonical_hourly_event_ticker

  legs = closed_legs_since_epoch(kalshi, since, asset=asset, fill_limit=fill_limit)
  by_event: dict[str, dict[str, Any]] = {}
  closed_pnls: list[float] = []
  wins = 0
  losses = 0
  for leg in legs:
    event = canonical_hourly_event_ticker(market_ticker_event_ticker(str(leg["ticker"])))
    if not event:
      continue
    pnl = float(leg.get("pnl_usd") or 0)
    row = by_event.setdefault(event, {"kalshi_pnl": 0.0, "kalshi_closed": 0})
    row["kalshi_pnl"] = round(float(row["kalshi_pnl"]) + pnl, 2)
    row["kalshi_closed"] = int(row["kalshi_closed"]) + 1
    closed_pnls.append(pnl)
    if pnl > 0:
      wins += 1
    elif pnl < 0:
      losses += 1
  total = round(sum(closed_pnls), 2)
  return {
    "ok": True,
    "by_event": by_event,
    "summary": {
      "ok": True,
      "pnl_source": PNL_SOURCE_KALSHI_WALLET,
      "closed_trades": len(closed_pnls),
      "total_pnl_usd": total,
      "wins": wins,
      "losses": losses,
      "win_rate": round(wins / len(closed_pnls), 3) if closed_pnls else None,
    },
  }


def kalshi_wallet_snapshot(
  kalshi: KalshiClient | None,
  cfg: dict[str, Any] | None,
  *,
  store: KalshiPortfolioPnlStore | None = None,
) -> dict[str, Any]:
  """Lightweight Kalshi wallet week KPI for embedding in other reports."""
  if not kalshi or not kalshi.authenticated:
    return {"ok": False, "pnl_source": PNL_SOURCE_KALSHI_WALLET, "error": "Kalshi not authenticated"}
  report = build_kalshi_portfolio_pnl_report_cached(kalshi, cfg, store=store)
  if not report.get("ok"):
    return {"ok": False, "pnl_source": PNL_SOURCE_KALSHI_WALLET, "error": report.get("error")}
  week = report.get("current_week") or {}
  runway = wallet_runway_kpi(week)
  return {
    **runway,
    "today_pnl_usd": round(float((report.get("today") or {}).get("total_pnl_usd") or 0), 2),
    "balance_usd": report.get("balance_usd"),
    "generated_at": report.get("generated_at"),
    "cached": report.get("cached"),
  }


def portfolio_activity_from_kalshi(
  kalshi: KalshiClient,
  *,
  fill_limit: int = 2000,
  settlement_limit: int = 2000,
) -> dict[str, list[dict[str, Any]]]:
  order_cache = _build_order_direction_cache(kalshi)
  raw_fills = kalshi.list_fills(limit=fill_limit, critical=True)
  raw_settlements = kalshi.list_settlements(limit=settlement_limit, critical=True)
  orders = _aggregate_fills_to_orders(raw_fills, order_cache=order_cache)

  entries: list[dict[str, Any]] = []
  for order in orders:
    if order.get("action") != "buy":
      continue
    ticker = str(order["ticker"])
    side = str(order["side"])
    contracts = max(1, int(round(float(order["contracts"]))))
    price_cents = int(order["price_cents"])
    bought_at = order.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
    order_id = str(order.get("order_id") or "")
    cost_usd = round(contracts * price_cents / 100.0, 2)
    entries.append({
      "fingerprint": _entry_fingerprint(order_id, ticker, side, bought_at, contracts),
      "order_id": order_id,
      "ticker": ticker,
      "side": side,
      "category": categorize_ticker(ticker),
      "contracts": contracts,
      "price_cents": price_cents,
      "cost_usd": cost_usd,
      "bought_at": bought_at,
    })

  legs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
  for order in orders:
    legs[(str(order["ticker"]), str(order["side"]))].append(order)

  closed: list[dict[str, Any]] = []
  settled_tickers: set[str] = set()
  for row in raw_settlements:
    ticker = str(row.get("ticker") or row.get("market_ticker") or "").strip()
    if not ticker:
      continue
    pnl = _settlement_net_pnl_usd(row)
    if pnl is None:
      continue
    exit_at = _settlement_created_at(row) or datetime.min.replace(tzinfo=timezone.utc)
    cost_usd = _settlement_entry_cost_usd(row)
    settled_tickers.add(ticker)
    closed.append({
      "fingerprint": _leg_fingerprint(ticker, "market", exit_at, exit_at, 0),
      "ticker": ticker,
      "side": "market",
      "category": categorize_ticker(ticker),
      "contracts": 0,
      "entry_cents": 0,
      "exit_cents": 0,
      "cost_usd": cost_usd,
      "pnl_usd": pnl,
      "exit_at": exit_at,
      "exit_type": "SETTLEMENT",
      "buy_at": exit_at,
    })

  for (ticker, side), leg_orders in legs.items():
    if ticker in settled_tickers:
      continue
    buys = [o for o in leg_orders if o["action"] == "buy"]
    sells = [o for o in leg_orders if o["action"] == "sell"]
    for leg in pair_fifo_closed_legs(buys, sells):
      buy_time = leg["buy_at"]
      sell_time = leg["exit_at"]
      contracts = leg["contracts"]
      closed.append({
        "fingerprint": _leg_fingerprint(ticker, side, buy_time, sell_time, contracts),
        "ticker": ticker,
        "side": side,
        "category": categorize_ticker(ticker),
        "contracts": contracts,
        "entry_cents": leg["entry_cents"],
        "exit_cents": leg["exit_cents"],
        "cost_usd": leg["cost_usd"],
        "pnl_usd": leg["pnl_usd"],
        "exit_at": sell_time,
        "exit_type": leg["exit_type"],
        "buy_at": buy_time,
      })
  return {"closed": closed, "entries": entries}


def closed_round_trips(
  kalshi: KalshiClient,
  *,
  fill_limit: int = 2000,
  settlement_limit: int = 2000,
) -> list[dict[str, Any]]:
  return portfolio_activity_from_kalshi(
    kalshi,
    fill_limit=fill_limit,
    settlement_limit=settlement_limit,
  )["closed"]


def sync_kalshi_portfolio_ledger(
  store: KalshiPortfolioPnlStore,
  kalshi: KalshiClient,
) -> dict[str, Any]:
  activity = portfolio_activity_from_kalshi(kalshi)
  closed_new = store.upsert_closed_legs(activity["closed"])
  entries_new = store.upsert_entries(activity["entries"])
  store.touch_sync()
  return {
    "closed_inserted": closed_new,
    "entries_inserted": entries_new,
    "closed_total": len(activity["closed"]),
    "entries_total": len(activity["entries"]),
  }


def _filter_since_epoch(
  closed: list[dict[str, Any]],
  entries: list[dict[str, Any]],
  epoch: datetime | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  if epoch is None:
    return closed, entries
  closed_f = [r for r in closed if _at_or_after_epoch(r.get("exit_at"), epoch)]
  entries_f = [r for r in entries if _at_or_after_epoch(r.get("bought_at"), epoch)]
  return closed_f, entries_f


def _derive_stats(
  closed: list[dict[str, Any]],
  entries: list[dict[str, Any]],
  *,
  start_et: datetime,
  end_et: datetime,
  label: str,
  now_et: datetime | None = None,
) -> dict[str, Any]:
  window_closed = [
    r for r in closed
    if _in_window_et(r.get("exit_at") or datetime.min.replace(tzinfo=timezone.utc), start_et, end_et)
  ]
  window_entries = [
    r for r in entries
    if _in_window_et(r.get("bought_at") or datetime.min.replace(tzinfo=timezone.utc), start_et, end_et)
  ]

  by_cat_pnl: dict[str, list[float]] = defaultdict(list)
  by_cat_entry_cost: dict[str, float] = defaultdict(float)
  by_cat_legs: dict[str, int] = defaultdict(int)
  by_cat_buys: dict[str, int] = defaultdict(int)

  for row in window_closed:
    cat = str(row.get("category") or "Other")
    by_cat_pnl[cat].append(float(row.get("pnl_usd") or 0))
    by_cat_entry_cost[cat] += float(row.get("cost_usd") or 0)
    by_cat_legs[cat] += 1
  for row in window_entries:
    cat = str(row.get("category") or "Other")
    by_cat_buys[cat] += 1

  cats = sorted(
    set(by_cat_pnl) | set(by_cat_entry_cost),
    key=lambda c: (-abs(sum(by_cat_pnl.get(c, []))), c),
  )
  by_category = []
  for cat in cats:
    pnls = by_cat_pnl.get(cat, [])
    n = len(pnls)
    total_pnl = round(sum(pnls), 2)
    entry_cost = round(by_cat_entry_cost.get(cat, 0.0), 2)
    wins = sum(1 for p in pnls if p > 0)
    by_category.append({
      "category": cat,
      "closed_legs": n,
      "entries": by_cat_buys.get(cat, 0),
      "total_pnl_usd": total_pnl,
      "invested_usd": entry_cost,
      "entry_cost_usd": entry_cost,
      "wins": wins,
      "losses": sum(1 for p in pnls if p < 0),
      "win_rate": round(wins / n, 3) if n else None,
      "pnl_per_leg_usd": round(total_pnl / n, 2) if n else None,
      "roi_pct": round(100.0 * total_pnl / entry_cost, 1) if entry_cost > 0 else None,
    })

  total_pnl = round(sum(float(r.get("pnl_usd") or 0) for r in window_closed), 2)
  total_entry_cost = round(sum(float(r.get("cost_usd") or 0) for r in window_closed), 2)
  total_buy_volume = round(sum(float(r.get("cost_usd") or 0) for r in window_entries), 2)
  closed_legs = len(window_closed)
  entry_count = len(window_entries)
  wins = sum(1 for r in window_closed if float(r.get("pnl_usd") or 0) > 0)
  losses = sum(1 for r in window_closed if float(r.get("pnl_usd") or 0) < 0)
  hours = _window_hours(start_et, end_et, now_et=now_et)

  return {
    "label": label,
    "pnl_source": PNL_SOURCE_KALSHI_WALLET,
    "window_start_et": start_et.isoformat(),
    "window_end_et": end_et.isoformat(),
    "closed_legs": closed_legs,
    "entries": entry_count,
    "wins": wins,
    "losses": losses,
    "win_rate": round(wins / closed_legs, 3) if closed_legs else None,
    "total_pnl_usd": total_pnl,
    "invested_usd": total_entry_cost,
    "entry_cost_usd": total_entry_cost,
    "buy_volume_usd": total_buy_volume,
    "pnl_per_leg_usd": round(total_pnl / closed_legs, 2) if closed_legs else None,
    "pnl_per_hour_usd": round(total_pnl / hours, 2),
    "roi_pct": round(100.0 * total_pnl / total_entry_cost, 1) if total_entry_cost > 0 else None,
    "active_hours": round(hours, 2),
    "by_category": by_category,
  }


def summarize_window(
  closed: list[dict[str, Any]],
  entries: list[dict[str, Any]],
  *,
  start_et: datetime,
  end_et: datetime,
  label: str,
  now_et: datetime | None = None,
) -> dict[str, Any]:
  return _derive_stats(
    closed,
    entries,
    start_et=start_et,
    end_et=end_et,
    label=label,
    now_et=now_et,
  )


def _sunday_for_date(d: date) -> date:
  return d - timedelta(days=(d.weekday() + 1) % 7)


def build_daily_history(
  closed: list[dict[str, Any]],
  entries: list[dict[str, Any]],
  *,
  now_et: datetime,
  limit: int = 60,
) -> list[dict[str, Any]]:
  day_keys: set[date] = set()
  for row in closed:
    day_keys.add(_to_utc(row["exit_at"]).astimezone(ET).date())
  for row in entries:
    day_keys.add(_to_utc(row["bought_at"]).astimezone(ET).date())

  out: list[dict[str, Any]] = []
  for day in sorted(day_keys, reverse=True)[:limit]:
    start, end, label = day_window_for_date(day)
    partial_now = now_et if day == now_et.date() else None
    block = _derive_stats(
      closed,
      entries,
      start_et=start,
      end_et=end,
      label=label,
      now_et=partial_now,
    )
    block["day"] = day.isoformat()
    block["is_today"] = day == now_et.date()
    out.append(block)
  return out


def build_weekly_history(
  closed: list[dict[str, Any]],
  entries: list[dict[str, Any]],
  *,
  now_et: datetime,
  limit: int = 26,
) -> list[dict[str, Any]]:
  week_starts: set[date] = set()
  for row in closed:
    d = _to_utc(row["exit_at"]).astimezone(ET).date()
    week_starts.add(_sunday_for_date(d))
  for row in entries:
    d = _to_utc(row["bought_at"]).astimezone(ET).date()
    week_starts.add(_sunday_for_date(d))

  current_sunday = _sunday_for_date(now_et.date())
  out: list[dict[str, Any]] = []
  for sunday in sorted(week_starts, reverse=True)[:limit]:
    start, end, label = week_window_for_sunday(sunday)
    partial_now = now_et if sunday == current_sunday else None
    block = _derive_stats(
      closed,
      entries,
      start_et=start,
      end_et=end,
      label=label,
      now_et=partial_now,
    )
    block["week_start"] = sunday.isoformat()
    block["is_current_week"] = sunday == current_sunday
    out.append(block)
  return out


def _position_exposure_usd(row: dict[str, Any]) -> float:
  val = row.get("market_exposure_dollars") or row.get("market_exposure")
  if val is None or val == "":
    return 0.0
  try:
    return round(float(val), 2)
  except (TypeError, ValueError):
    return 0.0


def build_kalshi_portfolio_pnl_report(
  kalshi: KalshiClient | None,
  *,
  store: KalshiPortfolioPnlStore | None = None,
  now: datetime | None = None,
  fill_limit: int = 2000,
  settlement_limit: int = 2000,
) -> dict[str, Any]:
  now_utc = _to_utc(now or datetime.now(timezone.utc))
  now_et = now_utc.astimezone(ET)
  if not kalshi or not kalshi.authenticated:
    return {
      "ok": False,
      "error": "Kalshi not authenticated",
      "generated_at": now_utc.isoformat(),
    }

  sync_meta: dict[str, Any] | None = None
  if store is not None:
    sync_meta = sync_kalshi_portfolio_ledger(store, kalshi)
    closed = store.list_closed_legs()
    entries = store.list_entries()
    runtime = store.runtime()
    epoch = parse_stats_epoch(runtime.get("stats_epoch_at"))
  else:
    activity = portfolio_activity_from_kalshi(
      kalshi,
      fill_limit=fill_limit,
      settlement_limit=settlement_limit,
    )
    closed = activity["closed"]
    entries = activity["entries"]
    runtime = {"stats_epoch_at": None, "last_sync_at": None, "clean_sheets": 0}
    epoch = None

  closed, entries = _filter_since_epoch(closed, entries, epoch)

  day_start, day_end, day_label = day_window_et(now_et)
  week_start, week_end, week_label = week_window_et(now_et)
  positions = kalshi.list_market_positions(critical=True)
  bal = kalshi.portfolio_balance() or {}
  balance_cents = kalshi.balance_cents_from_payload(bal)

  since_epoch_start = epoch.astimezone(ET) if epoch else None
  since_label = (
    since_epoch_start.strftime("%a %b %-d, %Y %H:%M ET")
    if since_epoch_start
    else "all recorded history"
  )

  week_block = summarize_window(
    closed,
    entries,
    start_et=week_start,
    end_et=week_end,
    label=week_label,
    now_et=now_et,
  )

  return {
    "ok": True,
    "pnl_source": PNL_SOURCE_KALSHI_WALLET,
    "generated_at": now_utc.isoformat(),
    "timezone": str(ET),
    "balance_usd": kalshi.balance_usd_from_cents(balance_cents),
    "open_positions_count": len(positions),
    "open_positions_exposure_usd": round(
      sum(_position_exposure_usd(p) for p in positions),
      2,
    ),
    "stats_epoch_at": runtime.get("stats_epoch_at"),
    "stats_epoch_label": since_label,
    "clean_sheets": int(runtime.get("clean_sheets") or 0),
    "last_sync_at": runtime.get("last_sync_at"),
    "sync": sync_meta,
    "ledger": {
      "closed_legs_stored": len(closed),
      "entries_stored": len(entries),
    },
    "since_epoch": summarize_window(
      closed,
      entries,
      start_et=since_epoch_start or datetime(1970, 1, 1, tzinfo=ET),
      end_et=now_et,
      label=since_label,
      now_et=now_et,
    ) if closed or entries else {
      "label": since_label,
      "total_pnl_usd": 0.0,
      "invested_usd": 0.0,
      "closed_legs": 0,
      "entries": 0,
      "by_category": [],
    },
    "today": summarize_window(
      closed,
      entries,
      start_et=day_start,
      end_et=day_end,
      label=day_label,
      now_et=now_et,
    ),
    "current_week": week_block,
    "week_runway": wallet_runway_kpi(week_block),
    "daily_history": build_daily_history(closed, entries, now_et=now_et),
    "weekly_history": build_weekly_history(closed, entries, now_et=now_et),
  }


def clean_sheet_kalshi_portfolio_pnl(
  store: KalshiPortfolioPnlStore,
  kalshi: KalshiClient | None,
  *,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  epoch = store.set_stats_epoch_now()
  invalidate_kalshi_portfolio_pnl_cache()
  report = build_kalshi_portfolio_pnl_report(kalshi, store=store)
  report["clean_sheet"] = {"ok": True, "stats_epoch_at": epoch}
  return report


def build_kalshi_portfolio_pnl_report_cached(
  kalshi: KalshiClient | None,
  cfg: dict[str, Any] | None = None,
  *,
  store: KalshiPortfolioPnlStore | None = None,
  ttl_sec: float = _REPORT_CACHE_TTL_SEC,
  now: datetime | None = None,
) -> dict[str, Any]:
  mono = time.monotonic()
  ledger = store or (kalshi_portfolio_pnl_store(cfg) if cfg is not None else None)
  cfg_key = str(ledger.db_path) if ledger else "ephemeral"
  cached = _REPORT_CACHE.get("payload")
  if (
    cached
    and _REPORT_CACHE.get("cfg_key") == cfg_key
    and (mono - float(_REPORT_CACHE.get("mono_at") or 0)) < ttl_sec
  ):
    return {**cached, "cached": True, "cache_age_sec": round(mono - float(_REPORT_CACHE["mono_at"]), 1)}

  payload = build_kalshi_portfolio_pnl_report(kalshi, store=ledger, now=now)
  _REPORT_CACHE["mono_at"] = mono
  _REPORT_CACHE["payload"] = payload
  _REPORT_CACHE["cfg_key"] = cfg_key
  return {**payload, "cached": False, "cache_age_sec": 0.0}


def invalidate_kalshi_portfolio_pnl_cache() -> None:
  _REPORT_CACHE["mono_at"] = 0.0
  _REPORT_CACHE["payload"] = None
  _REPORT_CACHE["cfg_key"] = None
