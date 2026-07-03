"""Backfill inverted NO exit P&L on historical bot_trades rows."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.trading.bot_risk_state import BotRiskCoordinator, bot_risk_key, daily_loss_config_from_cfg
from src.trading.paper_bankroll import apply_paper_exit_pnl, get_paper_state, migrate_paper_state
from src.trading.paper_execution import leg_pnl_usd

log = logging.getLogger(__name__)

_PNL_TOL = 0.015
_DB_NAME_RE = re.compile(r"^(hourly|slot15)_bot_(btc|eth)\.db$")


def wrong_no_exit_pnl_usd(
  *,
  entry_price_cents: int,
  exit_price_cents: int,
  contracts: int,
) -> float:
  """Pre-3.13.6 inverted NO formula: entry minus exit."""
  return round(contracts * (int(entry_price_cents) - int(exit_price_cents)) / 100.0, 2)


def correct_no_exit_pnl_usd(
  *,
  entry_price_cents: int,
  exit_price_cents: int,
  contracts: int,
) -> float:
  return float(
    leg_pnl_usd(
      entry_price_cents=entry_price_cents,
      mark_or_exit_cents=exit_price_cents,
      contracts=contracts,
    )
    or 0.0,
  )


def _pnl_close(a: float, b: float, *, tol: float = _PNL_TOL) -> bool:
  return abs(float(a) - float(b)) <= tol


def is_inverted_no_exit(row: dict[str, Any]) -> bool:
  if str(row.get("side") or "").lower() != "no":
    return False
  if str(row.get("action") or "") != "exit":
    return False
  if str(row.get("status") or "") != "filled":
    return False
  entry_c = row.get("entry_price_cents")
  exit_c = row.get("exit_price_cents")
  contracts = row.get("contracts")
  logged = row.get("pnl_usd")
  if entry_c is None or exit_c is None or contracts is None or logged is None:
    return False
  correct = correct_no_exit_pnl_usd(
    entry_price_cents=int(entry_c),
    exit_price_cents=int(exit_c),
    contracts=int(contracts),
  )
  wrong = wrong_no_exit_pnl_usd(
    entry_price_cents=int(entry_c),
    exit_price_cents=int(exit_c),
    contracts=int(contracts),
  )
  logged_f = round(float(logged), 2)
  if _pnl_close(logged_f, correct):
    return False
  return _pnl_close(logged_f, wrong)


def _parse_db_meta(db_path: Path) -> tuple[str, str] | None:
  m = _DB_NAME_RE.match(db_path.name)
  if not m:
    return None
  return m.group(1), m.group(2)


def _default_cap_from_settings(conn: sqlite3.Connection, kind: str) -> float:
  row = conn.execute("SELECT json FROM bot_settings WHERE id = 1").fetchone()
  if not row:
    return 25.0
  try:
    raw = json.loads(row[0])
  except (TypeError, json.JSONDecodeError):
    return 25.0
  if kind == "slot15":
    return float(raw.get("max_spend_per_slot_usd", raw.get("max_spend_per_hour_usd", 25.0)))
  return float(raw.get("max_spend_per_hour_usd", 25.0))


def _is_today_ny(created_at: str, *, tz_name: str = "America/New_York") -> bool:
  try:
    ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
  except ValueError:
    return False
  if ts.tzinfo is None:
    ts = ts.replace(tzinfo=timezone.utc)
  try:
    tz = ZoneInfo(tz_name)
  except Exception:
    tz = ZoneInfo("America/New_York")
  return ts.astimezone(tz).date() == datetime.now(tz).date()


def _find_inverted_no_exits(conn: sqlite3.Connection) -> list[dict[str, Any]]:
  rows = conn.execute(
    """
    SELECT id, pnl_usd, entry_price_cents, exit_price_cents, contracts, side,
           action, status, mode, created_at
    FROM bot_trades
    WHERE action = 'exit' AND LOWER(side) = 'no' AND status = 'filled'
      AND pnl_usd IS NOT NULL
      AND entry_price_cents IS NOT NULL
      AND exit_price_cents IS NOT NULL
      AND contracts IS NOT NULL
    """,
  ).fetchall()
  out: list[dict[str, Any]] = []
  for row in rows:
    d = dict(row)
    if is_inverted_no_exit(d):
      out.append(d)
  return out


def reconcile_daily_risk_deltas(
  data_dir: Path,
  deltas_by_bot: dict[str, float],
  *,
  cfg: dict[str, Any] | None = None,
  dry_run: bool = False,
) -> dict[str, float]:
  """Apply net P&L deltas to today's bot_daily_risk.json per bot."""
  if not deltas_by_bot:
    return {}
  applied: dict[str, float] = {}
  coord = BotRiskCoordinator(Path(data_dir), daily_loss_config_from_cfg(cfg))
  for bot_key, delta in sorted(deltas_by_bot.items()):
    if abs(delta) < _PNL_TOL:
      continue
    applied[bot_key] = round(delta, 2)
    if not dry_run:
      coord.record_exit_pnl(bot_key, delta)
  return applied


def today_live_exit_pnl_usd(
  db_path: Path | str,
  *,
  timezone: str = "America/New_York",
) -> float:
  """Sum live exit P&L for the current calendar day in *timezone*."""
  path = Path(db_path)
  if not path.is_file():
    return 0.0
  try:
    tz = ZoneInfo(timezone)
  except Exception:
    tz = ZoneInfo("America/New_York")
  today = datetime.now(tz).date()
  conn = sqlite3.connect(path)
  try:
    rows = conn.execute(
      """
      SELECT pnl_usd, created_at
      FROM bot_trades
      WHERE action = 'exit'
        AND mode = 'live'
        AND status IN ('filled', 'reconciled')
        AND pnl_usd IS NOT NULL
      """,
    ).fetchall()
  finally:
    conn.close()
  total = 0.0
  for pnl_usd, created_at in rows:
    if not _is_today_ny(str(created_at or ""), tz_name=timezone):
      continue
    total += float(pnl_usd or 0)
  return round(total, 2)


def sync_daily_risk_from_trade_logs(
  data_dir: Path | str,
  *,
  cfg: dict[str, Any] | None = None,
  dry_run: bool = False,
) -> dict[str, Any]:
  """
  Reconcile bot_daily_risk.json with today's live exit P&L in each bot DB.

  The daily risk file is incremental (updated on each exit). Historical bugs
  (inverted NO P&L, phantom settlements) can leave it stale vs the trade log.
  """
  root = Path(data_dir)
  dl_cfg = daily_loss_config_from_cfg(cfg)
  coord = BotRiskCoordinator(root, dl_cfg)
  per_bot: dict[str, Any] = {}
  changed = 0
  for db_path in bot_db_paths(root):
    meta = _parse_db_meta(db_path)
    if not meta:
      continue
    kind, asset = meta
    bot_key = bot_risk_key(kind, asset)
    computed = today_live_exit_pnl_usd(db_path, timezone=dl_cfg.timezone)
    before = coord.status_for_bot(bot_key)
    before_pnl = float(before.get("realized_pnl_usd") or 0)
    delta = round(computed - before_pnl, 2)
    per_bot[bot_key] = {
      "db_path": str(db_path),
      "computed_pnl_usd": computed,
      "before_pnl_usd": before_pnl,
      "delta_usd": delta,
    }
    if abs(delta) < _PNL_TOL:
      continue
    changed += 1
    if not dry_run:
      coord.sync_bot_realized_pnl(bot_key, computed)
      per_bot[bot_key]["after_pnl_usd"] = computed
  return {
    "data_dir": str(root),
    "date_key": coord._date_key,
    "timezone": dl_cfg.timezone,
    "bots_checked": len(per_bot),
    "bots_adjusted": changed,
    "per_bot": per_bot,
    "dry_run": dry_run,
  }


def backfill_bot_db(
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
    "fixed": 0,
    "paper_pnl_delta_usd": 0.0,
    "trade_ids": [],
    "dry_run": dry_run,
  }
  if not path.is_file():
    return stats

  meta = _parse_db_meta(path)
  kind = meta[0] if meta else "hourly"
  asset = meta[1] if meta else "btc"
  bot_key = bot_risk_key(kind, asset)

  paper_delta = 0.0
  daily_delta = 0.0
  conn = sqlite3.connect(path)
  conn.row_factory = sqlite3.Row
  try:
    migrate_paper_state(conn)
    candidates = _find_inverted_no_exits(conn)
    stats["scanned"] = len(candidates)
    if not candidates:
      return stats
    trade_ids: list[str] = []
    for row in candidates:
      entry_c = int(row["entry_price_cents"])
      exit_c = int(row["exit_price_cents"])
      contracts = int(row["contracts"])
      logged = round(float(row["pnl_usd"]), 2)
      correct = round(
        correct_no_exit_pnl_usd(
          entry_price_cents=entry_c,
          exit_price_cents=exit_c,
          contracts=contracts,
        ),
        2,
      )
      delta = round(correct - logged, 2)
      trade_ids.append(str(row["id"]))
      if row.get("mode") == "paper":
        paper_delta = round(paper_delta + delta, 2)
      if _is_today_ny(str(row.get("created_at") or "")):
        daily_delta = round(daily_delta + delta, 2)

      if not dry_run:
        conn.execute(
          "UPDATE bot_trades SET pnl_usd = ? WHERE id = ?",
          (correct, row["id"]),
        )

    stats["fixed"] = len(candidates)
    stats["trade_ids"] = trade_ids
    stats["paper_pnl_delta_usd"] = paper_delta
    stats["daily_risk_delta_usd"] = daily_delta

    if dry_run:
      return stats

    if paper_delta and get_paper_state(conn) is not None:
      default_cap = _default_cap_from_settings(conn, kind)
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


def bot_db_paths(
  data_dir: Path | str,
  *,
  assets: tuple[str, ...] = ("btc", "eth"),
  kinds: tuple[str, ...] = ("hourly", "slot15"),
) -> list[Path]:
  root = Path(data_dir)
  paths: list[Path] = []
  for asset in assets:
    asset = asset.lower()
    for logs_dir in (root / "logs", root / asset / "logs"):
      for kind in kinds:
        p = logs_dir / f"{kind}_bot_{asset}.db"
        if p.is_file():
          paths.append(p)
  return paths


def backfill_all_bot_dbs(
  data_dir: Path | str,
  *,
  assets: tuple[str, ...] = ("btc", "eth"),
  kinds: tuple[str, ...] = ("hourly", "slot15"),
  dry_run: bool = False,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  root = Path(data_dir)
  paths = bot_db_paths(root, assets=assets, kinds=kinds)
  per_db: list[dict[str, Any]] = []
  total_fixed = 0
  total_paper_delta = 0.0
  for db_path in paths:
    one = backfill_bot_db(db_path, dry_run=dry_run, data_dir=root, cfg=cfg)
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
