"""Void period-rollover exits logged before the market's real settle time."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.trading.bot_pnl_backfill import (
  _default_cap_from_settings,
  _is_today_ny,
  _parse_db_meta,
  bot_db_paths,
  reconcile_daily_risk_deltas,
)
from src.trading.hourly_event_time import (
  hourly_event_settle_utc,
  is_kalshi_hourly_event,
  market_ticker_event_ticker,
)
from src.trading.paper_bankroll import apply_paper_exit_pnl, get_paper_state, migrate_paper_state

log = logging.getLogger(__name__)

_DB_NAME_RE = re.compile(r"^(hourly|slot15)_bot_(btc|eth)\.db$")
_NY = ZoneInfo("America/New_York")


def _parse_created_at(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None
  if ts.tzinfo is None:
    ts = ts.replace(tzinfo=timezone.utc)
  return ts.astimezone(timezone.utc)


from src.trading.slot15_settlement import slot_period_settle_utc


def leg_settle_utc_for_trade(row: dict[str, Any]) -> datetime | None:
  """When the Kalshi market for this exit row actually settles."""
  market = str(row.get("market_ticker") or "")
  event = str(row.get("event_ticker") or "")
  leg_event = market_ticker_event_ticker(market) if market else event
  if is_kalshi_hourly_event(leg_event):
    return hourly_event_settle_utc(leg_event)
  if is_kalshi_hourly_event(event):
    return hourly_event_settle_utc(event)
  if event and ("T" in event or event.endswith("+00:00")):
    return slot_period_settle_utc(event)
  return None


def is_phantom_period_settlement(
  row: dict[str, Any],
  *,
  now: datetime | None = None,
) -> bool:
  """
  True when a PERIOD SETTLEMENT exit was logged before the leg's settle instant.

  These inflate realized P&L while Kalshi still holds the contracts.
  """
  if str(row.get("action") or "") != "exit":
    return False
  if str(row.get("trigger") or "") != "period_rollover":
    return False
  if str(row.get("status") or "") not in ("filled", "reconciled"):
    return False
  detail = str(row.get("detail") or "")
  if "PERIOD SETTLEMENT" not in detail:
    return False
  created = _parse_created_at(str(row.get("created_at") or ""))
  if created is None:
    return False
  settle = leg_settle_utc_for_trade(row)
  if settle is None:
    return False
  # Small grace: settlement index is published at the hour boundary.
  if created >= settle:
    return False
  now = now or datetime.now(timezone.utc)
  # Still before settle → definitely phantom.
  if now < settle:
    return True
  # Past settle but exit logged early → phantom (real settle wasn't available yet).
  return created < settle


def void_phantom_settlement_row(row: dict[str, Any]) -> tuple[float, str]:
  """Return (pnl_delta_to_apply, new_detail) for voiding a phantom exit."""
  logged = round(float(row.get("pnl_usd") or 0), 2)
  detail = str(row.get("detail") or "").rstrip()
  suffix = " [voided: logged before market settle — excluded from P&L]"
  if suffix.strip(" []") in detail:
    return 0.0, detail
  return -logged, detail + suffix


def cleanup_phantom_settlements_db(
  db_path: Path | str,
  *,
  dry_run: bool = False,
  data_dir: Path | None = None,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  path = Path(db_path)
  stats: dict[str, Any] = {
    "db_path": str(path),
    "exists": path.is_file(),
    "scanned": 0,
    "voided": 0,
    "pnl_removed_usd": 0.0,
    "paper_pnl_delta_usd": 0.0,
    "daily_risk_delta_usd": 0.0,
    "trade_ids": [],
    "dry_run": dry_run,
  }
  if not path.is_file():
    return stats

  meta = _parse_db_meta(path)
  if not meta:
    stats["skipped"] = "unknown_db"
    return stats
  bot_kind, asset = meta
  bot_key = f"{bot_kind}:{asset}"

  paper_delta = 0.0
  daily_delta = 0.0
  conn = sqlite3.connect(path)
  conn.row_factory = sqlite3.Row
  try:
    migrate_paper_state(conn)
    rows = conn.execute(
      """
      SELECT id, event_ticker, market_ticker, trigger, action, status, mode,
             side, contracts, pnl_usd, detail, created_at
      FROM bot_trades
      WHERE action = 'exit' AND trigger = 'period_rollover'
        AND status IN ('filled', 'reconciled')
      ORDER BY created_at ASC
      """,
    ).fetchall()
    stats["scanned"] = len(rows)
    trade_ids: list[str] = []
    pnl_removed = 0.0
    for raw in rows:
      row = dict(raw)
      if not is_phantom_period_settlement(row):
        continue
      delta, new_detail = void_phantom_settlement_row(row)
      trade_ids.append(str(row["id"]))
      pnl_removed = round(pnl_removed + float(row.get("pnl_usd") or 0), 2)
      if row.get("mode") == "paper":
        paper_delta = round(paper_delta + delta, 2)
      if _is_today_ny(str(row.get("created_at") or "")):
        daily_delta = round(daily_delta + delta, 2)
      if not dry_run:
        conn.execute(
          """
          UPDATE bot_trades
          SET status = 'voided', pnl_usd = 0, detail = ?
          WHERE id = ?
          """,
          (new_detail, row["id"]),
        )

    stats["voided"] = len(trade_ids)
    stats["trade_ids"] = trade_ids
    stats["pnl_removed_usd"] = pnl_removed
    stats["paper_pnl_delta_usd"] = paper_delta
    stats["daily_risk_delta_usd"] = daily_delta

    if dry_run or not trade_ids:
      return stats

    if paper_delta and get_paper_state(conn) is not None:
      default_cap = _default_cap_from_settings(conn, bot_kind)
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

  if trade_ids:
    log.info(
      "Voided %s phantom period-settlement exit(s) in %s (removed $%.2f bogus P&L)",
      len(trade_ids),
      path.name,
      pnl_removed,
    )
  return stats


def cleanup_all_phantom_settlement_dbs(
  data_dir: Path | str,
  *,
  dry_run: bool = False,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  root = Path(data_dir)
  paths = [p for p in bot_db_paths(root) if _DB_NAME_RE.match(p.name)]
  per_db: list[dict[str, Any]] = []
  total_voided = 0
  total_removed = 0.0
  for db_path in paths:
    one = cleanup_phantom_settlements_db(
      db_path,
      dry_run=dry_run,
      data_dir=root,
      cfg=cfg,
    )
    per_db.append(one)
    total_voided += int(one.get("voided") or 0)
    total_removed = round(total_removed + float(one.get("pnl_removed_usd") or 0), 2)
  return {
    "data_dir": str(root),
    "dbs_found": len(paths),
    "voided_count": total_voided,
    "pnl_removed_usd": total_removed,
    "dry_run": dry_run,
    "per_db": per_db,
  }
