"""Backfill hourly period-rollover exits that used market marks instead of settlement."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.trading.bot_pnl_backfill import (
  _PNL_TOL,
  _default_cap_from_settings,
  _is_today_ny,
  _parse_db_meta,
  bot_db_paths,
  reconcile_daily_risk_deltas,
)
from src.trading.hourly_settlement import (
  contract_spec_from_label,
  contract_spec_from_position,
  settlement_exit_cents,
)
from src.trading.paper_bankroll import apply_paper_exit_pnl, get_paper_state, migrate_paper_state
from src.trading.paper_execution import leg_pnl_usd

log = logging.getLogger(__name__)

_DB_NAME_RE = re.compile(r"^hourly_bot_(btc|eth)\.db$")


def _hourly_store_for_asset(cfg: dict[str, Any], asset: str):
  from src.assets import asset_cfg
  from src.db.hourly_store import create_hourly_store

  acfg = cfg if asset == "btc" else asset_cfg(cfg, asset)
  return create_hourly_store(acfg, asset=asset)


def settle_price_for_event(
  cfg: dict[str, Any],
  asset: str,
  event_ticker: str,
  *,
  cache: dict[tuple[str, str], float | None],
) -> float | None:
  key = (asset, event_ticker)
  if key in cache:
    return cache[key]
  price: float | None = None
  try:
    store = _hourly_store_for_asset(cfg, asset)
    row = store.get_by_event_ticker(event_ticker)
    if row and row.get("settle_brti") is not None:
      price = float(row["settle_brti"])
  except Exception as e:
    log.warning("Hourly settle lookup failed for %s %s: %s", asset, event_ticker, e)
  cache[key] = price
  return price


def contract_spec_for_trade(row: dict[str, Any]) -> dict[str, Any]:
  return contract_spec_from_position(row) or contract_spec_from_label(row.get("label"))


def correct_rollover_settlement(
  row: dict[str, Any],
  *,
  settle_price: float,
) -> tuple[int, float, str] | None:
  """Return (exit_cents, pnl_usd, detail) when settlement differs from stored exit."""
  if str(row.get("action") or "") != "exit":
    return None
  if str(row.get("trigger") or "") != "period_rollover":
    return None
  if str(row.get("status") or "") != "filled":
    return None
  entry_c = row.get("entry_price_cents")
  exit_c = row.get("exit_price_cents")
  contracts = row.get("contracts")
  side = str(row.get("side") or "")
  if entry_c is None or exit_c is None or contracts is None or not side:
    return None

  spec = contract_spec_for_trade(row)
  settled = settlement_exit_cents(
    side=side,
    settle_price=float(settle_price),
    spec=spec,
  )
  if settled is None:
    return None
  if int(exit_c) == int(settled):
    return None

  entry_i = int(entry_c)
  contracts_i = int(contracts)
  pnl = round(
    float(
      leg_pnl_usd(
        entry_price_cents=entry_i,
        mark_or_exit_cents=int(settled),
        contracts=contracts_i,
      )
      or 0.0,
    ),
    2,
  )
  idx = f"${float(settle_price):,.2f}"
  outcome = "won" if settled == 100 else "lost"
  from src.trading.bot_position_mode import exit_mode_label

  mode_label = exit_mode_label(row.get("mode"))
  detail = (
    f"{mode_label} EXIT (PERIOD SETTLEMENT): {side.upper()} ×{contracts_i} "
    f"@ {settled}¢ (entry {entry_i}¢) — settled @ {settled}¢ ({outcome} vs {idx}) [backfilled]"
  )
  return int(settled), pnl, detail


def is_market_rollover_exit(row: dict[str, Any]) -> bool:
  detail = str(row.get("detail") or "")
  if "PERIOD SETTLEMENT" in detail:
    return False
  if str(row.get("trigger") or "") != "period_rollover":
    return False
  return "PERIOD ROLLOVER" in detail or "forced close" in detail


def _find_rollover_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
  rows = conn.execute(
    """
    SELECT id, event_ticker, trigger, action, status, mode, side, contracts,
           price_cents, entry_price_cents, exit_price_cents, pnl_usd, label,
           detail, created_at
    FROM bot_trades
    WHERE action = 'exit' AND trigger = 'period_rollover' AND status = 'filled'
    """,
  ).fetchall()
  out: list[dict[str, Any]] = []
  for row in rows:
    d = dict(row)
    if is_market_rollover_exit(d):
      out.append(d)
  return out


def backfill_hourly_rollover_db(
  db_path: Path | str,
  *,
  dry_run: bool = False,
  data_dir: Path | None = None,
  cfg: dict[str, Any] | None = None,
  settle_cache: dict[tuple[str, str], float | None] | None = None,
) -> dict[str, Any]:
  path = Path(db_path)
  stats: dict[str, Any] = {
    "db_path": str(path),
    "exists": path.is_file(),
    "scanned": 0,
    "fixed": 0,
    "skipped_no_settle": 0,
    "paper_pnl_delta_usd": 0.0,
    "trade_ids": [],
    "dry_run": dry_run,
  }
  if not path.is_file():
    return stats

  meta = _parse_db_meta(path)
  if not meta or meta[0] != "hourly":
    stats["skipped"] = "not_hourly_db"
    return stats
  _, asset = meta
  bot_key = f"hourly:{asset}"
  cache = settle_cache if settle_cache is not None else {}

  paper_delta = 0.0
  daily_delta = 0.0
  conn = sqlite3.connect(path)
  conn.row_factory = sqlite3.Row
  try:
    migrate_paper_state(conn)
    candidates = _find_rollover_candidates(conn)
    stats["scanned"] = len(candidates)
    trade_ids: list[str] = []
    for row in candidates:
      event = str(row.get("event_ticker") or "")
      settle = settle_price_for_event(cfg or {}, asset, event, cache=cache)
      if settle is None:
        stats["skipped_no_settle"] += 1
        continue
      corrected = correct_rollover_settlement(row, settle_price=settle)
      if corrected is None:
        continue
      exit_c, pnl, detail = corrected
      logged_pnl = round(float(row.get("pnl_usd") or 0), 2)
      delta = round(pnl - logged_pnl, 2)
      trade_ids.append(str(row["id"]))
      if row.get("mode") == "paper":
        paper_delta = round(paper_delta + delta, 2)
      if _is_today_ny(str(row.get("created_at") or "")):
        daily_delta = round(daily_delta + delta, 2)
      if not dry_run:
        conn.execute(
          """
          UPDATE bot_trades
          SET price_cents = ?, exit_price_cents = ?, pnl_usd = ?, detail = ?
          WHERE id = ?
          """,
          (exit_c, exit_c, pnl, detail, row["id"]),
        )

    stats["fixed"] = len(trade_ids)
    stats["trade_ids"] = trade_ids
    stats["paper_pnl_delta_usd"] = paper_delta
    stats["daily_risk_delta_usd"] = daily_delta

    if dry_run or not trade_ids:
      return stats

    if paper_delta and get_paper_state(conn) is not None:
      default_cap = _default_cap_from_settings(conn, "hourly")
      apply_paper_exit_pnl(conn, paper_delta, default_cap)

    conn.commit()
  finally:
    conn.close()

  if not dry_run and data_dir is not None and daily_delta:
    applied = reconcile_daily_risk_deltas(
      Path(data_dir),
      {bot_key: daily_delta},
      cfg=cfg,
      dry_run=False,
    )
    stats["daily_risk_applied"] = applied

  return stats


def backfill_all_hourly_rollover_dbs(
  data_dir: Path | str,
  *,
  dry_run: bool = False,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  root = Path(data_dir)
  paths = [p for p in bot_db_paths(root) if _DB_NAME_RE.match(p.name)]
  cache: dict[tuple[str, str], float | None] = {}
  per_db: list[dict[str, Any]] = []
  total_fixed = 0
  total_paper_delta = 0.0
  for db_path in paths:
    one = backfill_hourly_rollover_db(
      db_path,
      dry_run=dry_run,
      data_dir=root,
      cfg=cfg,
      settle_cache=cache,
    )
    per_db.append(one)
    total_fixed += int(one.get("fixed") or 0)
    total_paper_delta = round(total_paper_delta + float(one.get("paper_pnl_delta_usd") or 0), 2)

  return {
    "data_dir": str(root),
    "dbs_found": len(paths),
    "fixed_count": total_fixed,
    "paper_pnl_delta_usd": total_paper_delta,
    "dry_run": dry_run,
    "per_db": per_db,
  }
