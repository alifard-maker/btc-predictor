#!/usr/bin/env python3
"""Track P&L-first Phase 0–1 milestone: N consecutive live hours with net P&L >= 0."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

DEFAULT_BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)
OUT_PATH = ROOT / "data" / "logs" / "pnl_first_milestone.json"


def _password() -> str:
  env_path = ROOT / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def _login(session: requests.Session, base: str) -> None:
  pw = _password()
  r = session.post(f"{base}/api/auth/login", data={"password": pw}, timeout=30)
  if r.status_code not in (200, 302, 303):
    raise RuntimeError(f"login failed: {r.status_code}")


def _hourly_intervals(session: requests.Session, base: str, *, asset: str, limit: int = 50) -> list[dict]:
  prefix = "/api/eth" if asset == "eth" else "/api"
  r = session.get(f"{base}{prefix}/hourly/bot/interval-history", params={"limit": limit}, timeout=60)
  body = r.json()
  return list(body.get("intervals") or body.get("history") or [])


def evaluate(
  session: requests.Session,
  base: str,
  *,
  asset: str,
  target_hours: int,
) -> dict:
  intervals = _hourly_intervals(session, base, asset=asset, limit=max(target_hours + 5, 30))
  live_rows = [
    row for row in intervals
    if str(row.get("mode") or "").lower() == "live"
  ]
  streak = 0
  streak_details: list[dict] = []
  for row in live_rows:
    try:
      pnl = float(row.get("net_pnl_usd") if row.get("net_pnl_usd") is not None else row.get("realized_pnl_usd") or 0)
    except (TypeError, ValueError):
      break
    if pnl >= 0:
      streak += 1
      streak_details.append({
        "event": row.get("event_ticker") or row.get("hour_label"),
        "net_pnl_usd": pnl,
      })
    else:
      break
    if streak >= target_hours:
      break

  achieved = streak >= target_hours
  return {
    "ts": datetime.now(timezone.utc).isoformat(),
    "asset": asset,
    "target_positive_hours": target_hours,
    "consecutive_positive_hours": streak,
    "milestone_achieved": achieved,
    "streak": streak_details[:target_hours],
  }


def main() -> int:
  parser = argparse.ArgumentParser(description="P&L-first milestone tracker")
  parser.add_argument("--base", default=DEFAULT_BASE)
  parser.add_argument("--asset", default="btc")
  parser.add_argument("--target-hours", type=int, default=20)
  args = parser.parse_args()

  session = requests.Session()
  _login(session, args.base)
  report = evaluate(session, args.base, asset=args.asset, target_hours=args.target_hours)
  OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
  OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

  status = "ACHIEVED" if report["milestone_achieved"] else "in progress"
  print(
    f"P&L-first milestone ({args.asset.upper()} live): "
    f"{report['consecutive_positive_hours']}/{args.target_hours} positive hours — {status}"
  )
  return 0 if report["milestone_achieved"] else 1


if __name__ == "__main__":
  raise SystemExit(main())
