"""Per-bot live tax exports sourced from Kalshi wallet (fills + settlements)."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backup.logs_backup import bot_db_specs, human_tax_bot_label, human_trade_db_specs
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

OTHER_BOT_LABEL = "kalshi_other"

# Category fallback when order_id is missing (primary live lane only — trials use order map).
PRIMARY_BOT_BY_CATEGORY: dict[str, str] = {
  "BTC hourly": "btc_hourly",
  "ETH hourly": "eth_hourly",
  "BTC 15m": "btc_slot15",
  "ETH 15m": "eth_slot15",
  "SPX hourly": "spx_hourly",
  "NDX hourly": "ndx_hourly",
}


def kalshi_client_for_backup(cfg: dict[str, Any] | None = None) -> KalshiClient | None:
  """Kalshi client for backups: running loop first, else config/env credentials."""
  try:
    from src.api import main as api_main

    loop = api_main._loop
    if loop is not None:
      client = getattr(loop, "kalshi", None)
      if client is not None and getattr(client, "authenticated", False):
        return client
  except Exception:
    pass
  try:
    from src.config import load_config

    client = KalshiClient(cfg or load_config())
    return client if client.authenticated else None
  except Exception:
    return None


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


def _human_order_rows_from_db(db_path: Path, mode: str) -> list[dict[str, Any]]:
  if not db_path.exists():
    return []
  import sqlite3

  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  conn.row_factory = sqlite3.Row
  try:
    rows = conn.execute(
      """
      SELECT kalshi_order_id FROM human_trades
      WHERE mode = ? AND kalshi_order_id IS NOT NULL AND action = 'enter'
      """,
      (mode,),
    ).fetchall()
    return [dict(r) for r in rows]
  except sqlite3.Error:
    return []
  finally:
    conn.close()


def build_live_kalshi_order_bot_map(cfg: dict[str, Any]) -> dict[str, str]:
  """Map Kalshi order_id → bot label (e.g. eth_hourly, btc_hourly_human) from live logs."""
  mapping: dict[str, str] = {}
  for asset, kind, db_path in bot_db_specs(cfg):
    label = f"{asset}_{kind}"
    for row in _rows_from_db(db_path, "live"):
      oid = str(row.get("kalshi_order_id") or "").strip()
      if oid:
        mapping[oid] = label
  for asset, db_path in human_trade_db_specs(cfg):
    label = human_tax_bot_label(asset)
    for row in _human_order_rows_from_db(db_path, "live"):
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
    bot = order_map[buy_oid]
    return [(bot, _tax_row(bot, leg))]

  if str(leg.get("exit_type") or "").upper() == "SETTLEMENT":
    ticker = str(leg.get("ticker") or "")
    bot_costs: dict[str, float] = defaultdict(float)
    for entry in entries:
      if str(entry.get("ticker") or "") != ticker:
        continue
      oid = str(entry.get("order_id") or "").strip()
      bot = order_map.get(oid)
      if bot:
        bot_costs[bot] += float(entry.get("cost_usd") or 0)
    if bot_costs:
      total_cost = sum(bot_costs.values())
      out: list[tuple[str, dict[str, Any]]] = []
      for bot, cost in bot_costs.items():
        share = (cost / total_cost) if total_cost > 0 else (1.0 / len(bot_costs))
        out.append((bot, _tax_row(bot, leg, share=share)))
      return out

  cat = str(leg.get("category") or "")
  primary = PRIMARY_BOT_BY_CATEGORY.get(cat)
  if primary:
    return [(primary, _tax_row(primary, leg))]

  return []


def _write_kalshi_tax_csv(rows: list[dict[str, Any]], path: Path) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with open(path, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=KALSHI_TAX_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for row in rows:
      w.writerow({k: row.get(k) for k in KALSHI_TAX_COLUMNS})


def write_tax_export_scaffold(cfg: dict[str, Any], dest: Path) -> list[str]:
  """Create per-bot trades.csv headers (empty) so tax folder layout is always visible."""
  written: list[str] = []
  for asset, kind, _db_path in bot_db_specs(cfg):
    label = f"{asset}_{kind}"
    path = dest / label / "trades.csv"
    _write_kalshi_tax_csv([], path)
    written.append(str(path.relative_to(dest)))
  for asset, _db_path in human_trade_db_specs(cfg):
    label = human_tax_bot_label(asset)
    path = dest / label / "trades.csv"
    _write_kalshi_tax_csv([], path)
    written.append(str(path.relative_to(dest)))
  other_path = dest / OTHER_BOT_LABEL / "trades.csv"
  _write_kalshi_tax_csv([], other_path)
  written.append(str(other_path.relative_to(dest)))
  return written


def write_tax_readme(dest: Path) -> None:
  dest.mkdir(parents=True, exist_ok=True)
  (dest / "TAX_README.txt").write_text(
    """Live tax exports — Kalshi wallet (exchange truth)
================================================

Each subfolder is one bot lane. Open trades.csv inside:

  btc_hourly/trades.csv     — BTC hourly live auto-bot (Kalshi wallet P&L)
  eth_hourly/trades.csv     — ETH hourly live auto-bot
  btc_hourly_human/trades.csv — BTC hourly manual dashboard trades (you)
  eth_hourly_human/trades.csv  — ETH hourly manual dashboard trades (you)
  btc_slot15/trades.csv     — BTC 15m
  eth_slot15/trades.csv     — ETH 15m
  kalshi_other/trades.csv   — Sports / other unattributed wallet legs

Column pnl_source is always kalshi_wallet (NOT bot SQLite).

bot_log_trades.csv — auto-bot strategy DB only — do not use for taxes.
human_log_trades.csv — manual trade ledger (features + bot counterfactual at click).
  Use trades.csv in *_hourly_human/ for tax P&L; human_log is audit/supporting detail.

Refresh: scheduled every 15 min on Railway, or:
  python3 scripts/backup_logs.py
  POST /api/admin/backup-logs  (with API key)

Requires Kalshi API credentials in the environment.
""",
    encoding="utf-8",
  )


def export_kalshi_wallet_live_trades(
  cfg: dict[str, Any],
  kalshi: KalshiClient | None,
  dest: Path,
) -> dict[str, Any]:
  """Write per-bot trades.csv from Kalshi wallet closed legs (tax source of truth)."""
  dest.mkdir(parents=True, exist_ok=True)
  scaffold = write_tax_export_scaffold(cfg, dest)
  write_tax_readme(dest)

  client = kalshi if kalshi and getattr(kalshi, "authenticated", False) else kalshi_client_for_backup(cfg)
  if not client or not getattr(client, "authenticated", False):
    return {
      "ok": False,
      "reason": "kalshi_not_authenticated",
      "pnl_source": PNL_SOURCE_KALSHI_WALLET,
      "scaffold_paths": scaffold,
      "message": "Created empty per-bot trades.csv headers — set Kalshi API credentials and re-run backup.",
    }

  order_map = build_live_kalshi_order_bot_map(cfg)
  activity = portfolio_activity_from_kalshi(client)
  per_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
  other_rows: list[dict[str, Any]] = []

  for leg in activity.get("closed") or []:
    assigned = attribute_closed_leg_to_bots(leg, activity.get("entries") or [], order_map)
    if not assigned:
      other_rows.append(_tax_row(OTHER_BOT_LABEL, leg))
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
    _write_kalshi_tax_csv(rows, dest / label / "trades.csv")
    all_rows.extend(rows)

  for asset, _db_path in human_trade_db_specs(cfg):
    label = human_tax_bot_label(asset)
    rows = per_bot.get(label, [])
    rows.sort(key=lambda r: str(r.get("exit_at") or ""))
    per_bot_counts[label] = len(rows)
    _write_kalshi_tax_csv(rows, dest / label / "trades.csv")
    all_rows.extend(rows)

  other_rows.sort(key=lambda r: str(r.get("exit_at") or ""))
  per_bot_counts[OTHER_BOT_LABEL] = len(other_rows)
  _write_kalshi_tax_csv(other_rows, dest / OTHER_BOT_LABEL / "trades.csv")
  all_rows.extend(other_rows)

  if all_rows:
    all_rows.sort(key=lambda r: str(r.get("exit_at") or ""))
    _write_kalshi_tax_csv(all_rows, dest / "all_trades.csv")

  tax_manifest = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "pnl_source": PNL_SOURCE_KALSHI_WALLET,
    "per_bot_counts": per_bot_counts,
    "total_trades": len(all_rows),
    "kalshi_order_map_size": len(order_map),
  }
  (dest / "tax_manifest.json").write_text(json.dumps(tax_manifest, indent=2), encoding="utf-8")

  return {
    "ok": True,
    "mode": "live",
    "pnl_source": PNL_SOURCE_KALSHI_WALLET,
    "total_trades": len(all_rows),
    "per_bot": per_bot_counts,
    "unattributed_closed_legs": len(other_rows),
    "kalshi_order_map_size": len(order_map),
    "scaffold_paths": scaffold,
    "tax_manifest": str(dest / "tax_manifest.json"),
  }
