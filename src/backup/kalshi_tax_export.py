"""Per-bot live tax exports sourced from Kalshi wallet (fills + settlements)."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backup.logs_backup import bot_db_specs
from src.data.kalshi import KalshiClient
from src.trading.kalshi_portfolio_pnl import PNL_SOURCE_KALSHI_WALLET, portfolio_activity_from_kalshi

KALSHI_TAX_COLUMNS = [
  "bot",
  "ticker",
  "side",
  "category",
  "contracts",
  "entry_price_cents",
  "exit_price_cents",
  "cost_usd",
  "pnl_usd",
  "buy_at",
  "exit_at",
  "exit_type",
  "kalshi_buy_order_id",
  "pnl_source",
]


def _rows_from_db(db_path: Path, mode: str) -> list[dict[str, Any]]:
  if not db_path.exists():
    return []
  import sqlite3

  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  conn.row_factory = sqlite3.Row
  try:
    rows = conn.execute(
      "SELECT kalshi_order_id FROM bot_trades WHERE mode = ? AND kalshi_order_id IS NOT NULL",
      (mode,),
    ).fetchall()
    return [dict(r) for r in rows]
  except sqlite3.Error:
    return []
  finally:
    conn.close()


def build_live_kalshi_order_bot_map(cfg: dict[str, Any]) -> dict[str, str]:
  """Map Kalshi order_id → bot label (e.g. eth_hourly) from live bot logs."""
  mapping: dict[str, str] = {}
  for asset, kind, db_path in bot_db_specs(cfg):
    label = f"{asset}_{kind}"
    for row in _rows_from_db(db_path, "live"):
      oid = str(row.get("kalshi_order_id") or "").strip()
      if oid:
        mapping[oid] = label
  return mapping


def _iso_ts(value: Any) -> str:
  if isinstance(value, datetime):
    return value.astimezone(timezone.utc).isoformat()
  return str(value or "")


def _tax_row(bot: str, leg: dict[str, Any], *, share: float = 1.0) -> dict[str, Any]:
  share = float(share)
  return {
    "bot": bot,
    "ticker": leg.get("ticker"),
    "side": leg.get("side"),
    "category": leg.get("category"),
    "contracts": leg.get("contracts"),
    "entry_price_cents": leg.get("entry_cents"),
    "exit_price_cents": leg.get("exit_cents"),
    "cost_usd": round(float(leg.get("cost_usd") or 0) * share, 2),
    "pnl_usd": round(float(leg.get("pnl_usd") or 0) * share, 2),
    "buy_at": _iso_ts(leg.get("buy_at")),
    "exit_at": _iso_ts(leg.get("exit_at")),
    "exit_type": leg.get("exit_type"),
    "kalshi_buy_order_id": leg.get("buy_order_id") or "",
    "pnl_source": PNL_SOURCE_KALSHI_WALLET,
  }


def attribute_closed_leg_to_bots(
  leg: dict[str, Any],
  entries: list[dict[str, Any]],
  order_map: dict[str, str],
) -> list[tuple[str, dict[str, Any]]]:
  """Assign a closed Kalshi leg to one or more bots (proportional on settlements)."""
  buy_oid = str(leg.get("buy_order_id") or "").strip()
  if buy_oid and buy_oid in order_map:
    return [(order_map[buy_oid], _tax_row(order_map[buy_oid], leg))]

  if str(leg.get("exit_type") or "").upper() != "SETTLEMENT":
    return []

  ticker = str(leg.get("ticker") or "")
  bot_costs: dict[str, float] = defaultdict(float)
  for entry in entries:
    if str(entry.get("ticker") or "") != ticker:
      continue
    oid = str(entry.get("order_id") or "").strip()
    bot = order_map.get(oid)
    if bot:
      bot_costs[bot] += float(entry.get("cost_usd") or 0)

  if not bot_costs:
    return []

  total_cost = sum(bot_costs.values())
  out: list[tuple[str, dict[str, Any]]] = []
  for bot, cost in bot_costs.items():
    share = (cost / total_cost) if total_cost > 0 else (1.0 / len(bot_costs))
    out.append((bot, _tax_row(bot, leg, share=share)))
  return out


def _write_kalshi_tax_csv(rows: list[dict[str, Any]], path: Path) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with open(path, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=KALSHI_TAX_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for row in rows:
      w.writerow({k: row.get(k) for k in KALSHI_TAX_COLUMNS})


def export_kalshi_wallet_live_trades(
  cfg: dict[str, Any],
  kalshi: KalshiClient,
  dest: Path,
) -> dict[str, Any]:
  """Write per-bot trades.csv from Kalshi wallet closed legs (tax source of truth)."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": False, "reason": "kalshi_not_authenticated", "pnl_source": PNL_SOURCE_KALSHI_WALLET}

  order_map = build_live_kalshi_order_bot_map(cfg)
  activity = portfolio_activity_from_kalshi(kalshi)
  per_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
  unattributed = 0

  for leg in activity.get("closed") or []:
    assigned = attribute_closed_leg_to_bots(leg, activity.get("entries") or [], order_map)
    if not assigned:
      unattributed += 1
      continue
    for bot, row in assigned:
      per_bot[bot].append(row)

  all_rows: list[dict[str, Any]] = []
  per_bot_counts: dict[str, int] = {}
  for asset, kind, _db_path in bot_db_specs(cfg):
    label = f"{asset}_{kind}"
    rows = per_bot.get(label, [])
    rows.sort(key=lambda r: str(r.get("exit_at") or ""))
    per_bot_counts[label] = len(rows)
    if rows:
      _write_kalshi_tax_csv(rows, dest / label / "trades.csv")
      all_rows.extend(rows)

  if all_rows:
    all_rows.sort(key=lambda r: str(r.get("exit_at") or ""))
    _write_kalshi_tax_csv(all_rows, dest / "all_trades.csv")

  return {
    "ok": True,
    "mode": "live",
    "pnl_source": PNL_SOURCE_KALSHI_WALLET,
    "total_trades": len(all_rows),
    "per_bot": per_bot_counts,
    "unattributed_closed_legs": unattributed,
    "kalshi_order_map_size": len(order_map),
  }
