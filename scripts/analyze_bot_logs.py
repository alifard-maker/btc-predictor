#!/usr/bin/env python3
"""Analyze bot trade logs — compare since-reset vs prior, by signal and entry quality."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.trading.bot_performance_report import _closed_round_trips, build_bot_performance_report


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _load_trades(db_path: Path) -> list[dict]:
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  conn.row_factory = sqlite3.Row
  try:
    rows = conn.execute(
      "SELECT * FROM bot_trades ORDER BY created_at ASC",
    ).fetchall()
    return [dict(r) for r in rows]
  finally:
    conn.close()


def _paper_reset_at(db_path: Path) -> str | None:
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  try:
    row = conn.execute(
      "SELECT paper_bankroll_started_at FROM bot_paper_state WHERE id = 1",
    ).fetchone()
    return str(row[0]) if row else None
  except sqlite3.Error:
    return None
  finally:
    conn.close()


def _settings(db_path: Path) -> dict:
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  try:
    row = conn.execute("SELECT json FROM bot_settings WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else {}
  except sqlite3.Error:
    return {}
  finally:
    conn.close()


def _split_trades(trades: list[dict], reset_at: str | None) -> tuple[list[dict], list[dict]]:
  if not reset_at:
    return trades, []
  reset_dt = _parse_ts(reset_at)
  if reset_dt is None:
    return trades, []
  before: list[dict] = []
  since: list[dict] = []
  for t in trades:
    ts = _parse_ts(t.get("created_at"))
    if ts is None or ts >= reset_dt:
      since.append(t)
    else:
      before.append(t)
  return before, since


def _exit_summary(trades: list[dict]) -> dict:
  closed = _closed_round_trips(trades)
  if not closed:
    return {"closed": 0, "pnl": 0.0, "wins": 0, "losses": 0, "win_rate": None}
  pnls = [float(r["pnl_usd"]) for r in closed]
  wins = sum(1 for p in pnls if p > 0)
  n = len(pnls)
  return {
    "closed": n,
    "pnl": round(sum(pnls), 2),
    "wins": wins,
    "losses": n - wins,
    "win_rate": round(wins / n, 3),
    "avg_pnl": round(sum(pnls) / n, 2),
  }


def _by_label(trades: list[dict]) -> list[dict]:
  closed = _closed_round_trips(trades)
  by: dict[str, list[float]] = defaultdict(list)
  enters: dict[str, dict] = {}
  for t in trades:
    if t.get("action") == "enter" and t.get("position_id"):
      enters[str(t["position_id"])] = t
  for r in closed:
    pid = str(r.get("position_id") or "")
    ent = enters.get(pid, {})
    label = str(ent.get("label") or ent.get("signal") or r.get("signal") or "unknown")
    by[label].append(float(r["pnl_usd"]))
  rows = []
  for label, pnls in sorted(by.items(), key=lambda x: -len(x[1])):
    n = len(pnls)
    rows.append({
      "label": label,
      "trades": n,
      "pnl": round(sum(pnls), 2),
      "avg": round(sum(pnls) / n, 2),
      "win_rate": round(sum(1 for p in pnls if p > 0) / n, 3),
    })
  return rows


def analyze_db(db_path: Path, *, label: str, min_ask_edge: float) -> dict:
  trades = _load_trades(db_path)
  reset_at = _paper_reset_at(db_path)
  before, since = _split_trades(trades, reset_at)
  settings = _settings(db_path)
  report_since = build_bot_performance_report(
    kind="slot15" if "slot15" in db_path.name else "hourly",
    asset="eth" if "_eth" in db_path.name else "btc",
    trades=since,
    min_ask_edge_cents=min_ask_edge,
  )
  return {
    "label": label,
    "db": str(db_path),
    "settings": {
      "enabled": settings.get("enabled"),
      "mode": settings.get("mode"),
      "allow_strong": settings.get("allow_strong"),
      "allow_actionable": settings.get("allow_actionable"),
      "free_mode": not settings.get("allow_strong") and not settings.get("allow_actionable"),
      "max_cap": settings.get("max_spend_per_slot_usd") or settings.get("max_spend_per_hour_usd"),
    },
    "paper_reset_at": reset_at,
    "all_time": _exit_summary(trades),
    "before_reset": _exit_summary(before),
    "since_reset": _exit_summary(since),
    "since_reset_by_label": _by_label(since),
    "performance_report_since_reset": report_since,
    "open_enters_since_reset": sum(
      1 for t in since if t.get("action") == "enter" and t.get("status") == "filled"
    ),
  }


def main() -> int:
  p = argparse.ArgumentParser(description="Analyze bot SQLite trade logs")
  p.add_argument("--data-dir", default=None, help="DATA_DIR (default: ./data or /data)")
  p.add_argument("--json", action="store_true")
  args = p.parse_args()
  data = Path(args.data_dir or __import__("os").environ.get("DATA_DIR", "data"))
  specs = [
    (data / "eth" / "logs" / "slot15_bot_eth.db", "ETH 15m", 5.0),
    (data / "eth" / "logs" / "hourly_bot_eth.db", "ETH hourly", 8.0),
    (data / "logs" / "slot15_bot_btc.db", "BTC 15m", 5.0),
    (data / "logs" / "hourly_bot_btc.db", "BTC hourly", 8.0),
  ]
  out = []
  for path, label, edge in specs:
    if path.exists():
      out.append(analyze_db(path, label=label, min_ask_edge=edge))
  if not out:
    print("No bot DBs found under", data, file=sys.stderr)
    return 1
  if args.json:
    print(json.dumps(out, indent=2))
    return 0
  for block in out:
    print(f"\n=== {block['label']} ===")
    s = block["settings"]
    print(f"  free_mode={s.get('free_mode')} enabled={s.get('enabled')} max_cap={s.get('max_cap')}")
    print(f"  paper_reset_at: {block.get('paper_reset_at')}")
    for period in ("all_time", "before_reset", "since_reset"):
      sm = block[period]
      if sm["closed"] or period == "since_reset":
        wr = f"{sm['win_rate'] * 100:.1f}%" if sm.get("win_rate") is not None else "—"
        print(f"  {period}: {sm['closed']} closed, PnL ${sm['pnl']:+.2f}, WR {wr}")
    print("  since_reset by entry label:")
    for row in block.get("since_reset_by_label") or []:
      print(f"    {row['label']}: {row['trades']} trades, ${row['pnl']:+.2f}, avg ${row['avg']:+.2f}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
