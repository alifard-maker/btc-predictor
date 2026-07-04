#!/usr/bin/env python3
"""Pull last N hours of live vs trial trades; split Strategy 1 vs 2."""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from urllib.parse import urljoin

import urllib.parse
import urllib.request

BASE = os.getenv("ANALYZE_BASE_URL", "https://btc-predictor-production-f460.up.railway.app")
HOURS = float(os.getenv("ANALYZE_HOURS", "10"))
PASSWORD = os.getenv("APP_PASSWORD", "")

PAIRS = (
  ("btc", "hourly", "hourly_trial", "/api/hourly/bot/trades", "/api/hourly-trial/bot/trades"),
  ("eth", "hourly", "hourly_trial", "/api/eth/hourly/bot/trades", "/api/eth/hourly-trial/bot/trades"),
)


def _classify_strategy(trade: dict) -> str:
  label = str(trade.get("label") or "")
  detail = str(trade.get("detail") or "")
  mt = str(trade.get("market_ticker") or "")
  if " to " in label.lower() and "$" in label:
    return "strategy_2_range"
  if "-B" in mt.upper() and re.search(r"-B\d", mt.upper()):
    return "strategy_2_range"
  if re.search(r"\$[\d,]+ or (above|below)", label, re.I):
    return "strategy_1_threshold"
  if "-T" in mt.upper():
    return "strategy_1_threshold"
  return "unknown"


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _login(opener: urllib.request.OpenerDirector) -> None:
  if not PASSWORD:
    raise SystemExit("APP_PASSWORD not set (use railway run)")
  data = urllib.parse.urlencode({"password": PASSWORD}).encode()
  req = urllib.request.Request(
    urljoin(BASE, "/api/auth/login"),
    data=data,
    method="POST",
    headers={"Content-Type": "application/x-www-form-urlencoded"},
  )
  opener.open(req, timeout=30)


def _get_json(opener: urllib.request.OpenerDirector, path: str) -> list | dict:
  req = urllib.request.Request(urljoin(BASE, path))
  with opener.open(req, timeout=60) as resp:
    return json.loads(resp.read().decode())


def _filter_since(trades: list[dict], since: datetime) -> list[dict]:
  out = []
  for t in trades:
    ts = _parse_ts(t.get("created_at"))
    if ts and ts >= since:
      out.append(t)
  return out


def _summarize(trades: list[dict], *, mode: str | None = None) -> dict:
  if mode:
    trades = [t for t in trades if str(t.get("mode") or "").lower() == mode]
  enters = [t for t in trades if t.get("action") == "enter" and t.get("status") == "filled"]
  exits = [
    t for t in trades
    if t.get("action") == "exit" and str(t.get("status") or "").lower() in ("filled", "reconciled")
  ]
  pnl = 0.0
  wins = losses = 0
  for e in exits:
    v = e.get("pnl_usd")
    if v is None:
      continue
    pnl += float(v)
    if float(v) > 0:
      wins += 1
    elif float(v) < 0:
      losses += 1
  by_strat: dict[str, dict] = defaultdict(lambda: {
    "enters": 0, "exits": 0, "pnl": 0.0, "wins": 0, "losses": 0,
  })
  for e in enters:
    s = _classify_strategy(e)
    by_strat[s]["enters"] += 1
  for e in exits:
    s = _classify_strategy(e)
    by_strat[s]["exits"] += 1
    v = e.get("pnl_usd")
    if v is not None:
      by_strat[s]["pnl"] += float(v)
      if float(v) > 0:
        by_strat[s]["wins"] += 1
      elif float(v) < 0:
        by_strat[s]["losses"] += 1
  return {
    "enter_fills": len(enters),
    "exit_fills": len(exits),
    "realized_pnl": round(pnl, 2),
    "wins": wins,
    "losses": losses,
    "win_rate": round(100 * wins / (wins + losses), 1) if wins + losses else None,
    "by_strategy": {k: {
      **v,
      "pnl": round(v["pnl"], 2),
      "win_rate": round(100 * v["wins"] / (v["wins"] + v["losses"]), 1)
      if v["wins"] + v["losses"] else None,
    } for k, v in sorted(by_strat.items())},
  }


def main() -> None:
  since = datetime.now(timezone.utc) - timedelta(hours=HOURS)
  jar = CookieJar()
  opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
  _login(opener)

  print(f"# Last {HOURS:.0f}h performance (since {since.isoformat()})")
  print(f"# Base: {BASE}\n")

  for asset, live_kind, trial_kind, live_path, trial_path in PAIRS:
    live_trades = _get_json(opener, f"{live_path}?limit=500")
    trial_trades = _get_json(opener, f"{trial_path}?limit=500")
    if isinstance(live_trades, dict):
      live_trades = live_trades.get("trades") or live_trades.get("hour_trades") or []
    if isinstance(trial_trades, dict):
      trial_trades = trial_trades.get("trades") or trial_trades.get("hour_trades") or []

    live_recent = _filter_since(live_trades, since)
    trial_recent = _filter_since(trial_trades, since)

    live_sum = _summarize(live_recent, mode="live")
    trial_sum = _summarize(trial_recent, mode="paper")

    print(f"## {asset.upper()} — Live vs Standard Trial")
    print(json.dumps({
      "live": live_sum,
      "trial_paper": trial_sum,
      "live_minus_trial_pnl": round(live_sum["realized_pnl"] - trial_sum["realized_pnl"], 2),
    }, indent=2))
    print()


if __name__ == "__main__":
  main()
