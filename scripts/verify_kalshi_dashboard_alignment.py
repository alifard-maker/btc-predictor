#!/usr/bin/env python3
"""Compare Kalshi fill P&L vs dashboard compare card and interval stats.

Designed to run after 4pm EDT on a trading day (e.g. Jul 4 2026) once the
1pm–3pm EDT hours have settled. Uses production session auth + Kalshi fills
on the server side.

Usage:
  python scripts/verify_kalshi_dashboard_alignment.py
  python scripts/verify_kalshi_dashboard_alignment.py --hours 13,14,15 --date 2026-07-04
  python scripts/verify_kalshi_dashboard_alignment.py --sync   # force fill sync first
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.trading.hourly_event_time import canonical_hourly_event_ticker, hourly_event_settle_utc
from src.trading.paper_execution import leg_pnl_usd

BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)
DEFAULT_STATS_EPOCH = "2026-07-04T16:59:00+00:00"  # 12:59 PM EDT


def _password() -> str:
  env_path = ROOT / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def _login(session: requests.Session) -> None:
  r = session.post(f"{BASE}/api/auth/login", data={"password": _password()}, timeout=30)
  if r.status_code not in (200, 302, 303):
    raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]}")


def _event_ticker_for_hour(date_str: str, hour_edt: int) -> str:
  """Build canonical KXBTCD event ticker for an EDT hour on date_str (YYYY-MM-DD)."""
  dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
    hour=hour_edt,
    minute=0,
    second=0,
    tzinfo=ZoneInfo("America/New_York"),
  )
  suffix = dt.strftime("%y%b%d%H").upper()
  return f"KXBTCD-{suffix}"


def _bot_hour_pnl(session: requests.Session, base_url: str, event_ticker: str) -> dict[str, Any]:
  trades = session.get(
    f"{base_url}/api/hourly/bot/trades?limit=200&event_ticker={event_ticker}",
    timeout=60,
  ).json()
  live_exits = [
    t for t in trades.get("trades") or []
    if t.get("mode") == "live"
    and t.get("action") == "exit"
    and str(t.get("status") or "") in ("filled", "reconciled")
  ]
  from src.trading.bot_exit_pnl import effective_exit_pnl_usd

  scratch_count = sum(
    1 for t in live_exits
    if float(t.get("pnl_usd") or 0) == 0
    and str(t.get("status") or "") == "reconciled"
  )
  pnl_sum = round(sum(effective_exit_pnl_usd(t) for t in live_exits), 2)
  return {
    "event_ticker": canonical_hourly_event_ticker(event_ticker),
    "exit_count": len(live_exits),
    "scratch_reconciled_count": scratch_count,
    "bot_pnl_usd": pnl_sum,
  }


def _compare_hour_row(compare: dict[str, Any], event_ticker: str) -> dict[str, Any] | None:
  canon = canonical_hourly_event_ticker(event_ticker)
  for hour in compare.get("hours") or []:
    if canonical_hourly_event_ticker(hour.get("event_ticker") or "") == canon:
      return hour
  return None


def build_report(
  session: requests.Session,
  *,
  base_url: str,
  date_str: str,
  hours_edt: list[int],
  stats_epoch: str,
  sync_fills: bool,
) -> dict[str, Any]:
  if sync_fills:
    sync_resp = session.post(f"{base_url}/api/hourly/bot/sync-kalshi-fills", timeout=120).json()
  else:
    sync_resp = None

  compare = session.get(
    f"{base_url}/api/bots/hourly-live-trial-compare?asset=btc&limit_hours=24",
    timeout=60,
  ).json()
  bot = session.get(f"{base_url}/api/hourly/bot", timeout=60).json()
  kalshi_summary: dict[str, Any] = {}
  kalshi_resp = session.get(
    f"{base_url}/api/hourly/bot/kalshi-fill-summary?since={quote(stats_epoch, safe='')}",
    timeout=60,
  )
  if kalshi_resp.status_code == 200:
    kalshi_summary = kalshi_resp.json()
  else:
    kalshi_summary = {
      "ok": False,
      "error": f"HTTP {kalshi_resp.status_code}",
      "hint": "Deploy latest code or pass --sync after deploy for fill summary endpoint",
    }

  interval_perf = bot.get("interval_performance") or {}
  hour_rows: list[dict[str, Any]] = []
  mismatches: list[dict[str, Any]] = []

  for hour in hours_edt:
    evt = _event_ticker_for_hour(date_str, hour)
    bot_hour = _bot_hour_pnl(session, base_url, evt)
    cmp_row = _compare_hour_row(compare, evt)
    compare_live_pnl = float((cmp_row or {}).get("live", {}).get("net_pnl_usd") or 0)
    settle = hourly_event_settle_utc(evt)
    row = {
      "hour_edt": hour,
      "event_ticker": evt,
      "settle_utc": settle.isoformat() if settle else None,
      "in_stats_epoch": bool(settle and settle > datetime.fromisoformat(stats_epoch.replace("Z", "+00:00"))),
      "bot_trade_log_pnl_usd": bot_hour["bot_pnl_usd"],
      "compare_card_live_pnl_usd": compare_live_pnl,
      "exit_count": bot_hour["exit_count"],
      "scratch_reconciled_exits": bot_hour["scratch_reconciled_count"],
    }
    if abs(compare_live_pnl - bot_hour["bot_pnl_usd"]) > 0.02:
      mismatches.append({
        **row,
        "issue": "compare_card_vs_trade_log",
        "delta_usd": round(compare_live_pnl - bot_hour["bot_pnl_usd"], 2),
      })
    hour_rows.append(row)

  return {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "base_url": base_url,
    "date": date_str,
    "hours_edt": hours_edt,
    "stats_epoch_at": compare.get("stats_epoch_at") or stats_epoch,
    "sync_fills": sync_resp,
    "kalshi_fill_summary_since_epoch": kalshi_summary,
    "interval_performance": {
      "net_interval_pnl_usd": interval_perf.get("net_interval_pnl_usd"),
      "profit_intervals": interval_perf.get("profit_intervals"),
      "loss_intervals": interval_perf.get("loss_intervals"),
      "breakeven_intervals": interval_perf.get("breakeven_intervals"),
      "intervals_pending": interval_perf.get("intervals_pending"),
      "stats_epoch_at": interval_perf.get("stats_epoch_at"),
    },
    "hours": hour_rows,
    "mismatches": mismatches,
    "notes": [
      "Kalshi fill summary is exchange round-trip P&L since stats_epoch (all hours combined).",
      "Per-hour bot P&L uses effective_exit_pnl (repairs pnl_usd=0 when entry≠exit).",
      "Scratch reconciled exits (pnl=0, entry=exit) usually mean sell fills were not paired.",
    ],
  }


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--date", default="2026-07-04", help="Trading date (YYYY-MM-DD, America/New_York)")
  parser.add_argument("--hours", default="13,14,15", help="EDT hours to check (comma-separated)")
  parser.add_argument("--stats-epoch", default=DEFAULT_STATS_EPOCH, help="Stats epoch ISO-8601 UTC")
  parser.add_argument("--sync", action="store_true", help="POST sync-kalshi-fills before compare")
  parser.add_argument("--base", default=BASE, help="Production base URL")
  args = parser.parse_args()

  base_url = args.base

  if not _password():
    print("Set APP_PASSWORD in .env", file=sys.stderr)
    return 1

  hours = [int(h.strip()) for h in args.hours.split(",") if h.strip()]
  session = requests.Session()
  _login(session)
  report = build_report(
    session,
    base_url=base_url,
    date_str=args.date,
    hours_edt=hours,
    stats_epoch=args.stats_epoch,
    sync_fills=args.sync,
  )
  print(json.dumps(report, indent=2))
  return 0 if not report.get("mismatches") else 2


if __name__ == "__main__":
  raise SystemExit(main())
