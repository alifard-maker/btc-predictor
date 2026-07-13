#!/usr/bin/env python3
"""Kalshi portfolio realized P&L by category (fills + settlements)."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.data.kalshi import KalshiClient, load_kalshi_config
from src.trading.kalshi_portfolio_pnl import (
  build_kalshi_portfolio_pnl_report,
  closed_round_trips,
)


def _to_utc(dt: datetime) -> datetime:
  if dt.tzinfo is None:
    return dt.replace(tzinfo=timezone.utc)
  return dt.astimezone(timezone.utc)


def _summarize_rolling(closed, *, since: datetime) -> dict:
  since_utc = _to_utc(since)
  window = [
    r for r in closed
    if _to_utc(r.get("exit_at") or datetime.min.replace(tzinfo=timezone.utc)) >= since_utc
  ]
  by_cat: dict[str, list[float]] = defaultdict(list)
  for row in window:
    by_cat[str(row.get("category") or "Other")].append(float(row.get("pnl_usd") or 0))
  cats = []
  for cat, pnls in sorted(by_cat.items(), key=lambda kv: (-abs(sum(kv[1])), kv[0])):
    n = len(pnls)
    total = round(sum(pnls), 2)
    wins = sum(1 for p in pnls if p > 0)
    cats.append({
      "category": cat,
      "closed_legs": n,
      "total_pnl_usd": total,
      "wins": wins,
      "losses": sum(1 for p in pnls if p < 0),
      "win_rate": round(wins / n, 3) if n else None,
    })
  total = round(sum(float(r.get("pnl_usd") or 0) for r in window), 2)
  return {
    "since": since_utc.isoformat(),
    "closed_legs": len(window),
    "total_pnl_usd": total,
    "by_category": cats,
  }


def main() -> int:
  parser = argparse.ArgumentParser(description="Kalshi realized P&L report")
  parser.add_argument("--json", action="store_true")
  parser.add_argument("--calendar", action="store_true", help="ET calendar day + Sunday week")
  args = parser.parse_args()

  cfg = load_config()
  cfg["kalshi"] = load_kalshi_config(cfg)
  kalshi = KalshiClient(cfg)
  if not kalshi or not kalshi.authenticated:
    print("Kalshi not authenticated (set KALSHI_KEY_ID + KALSHI_PRIVATE_KEY)", file=sys.stderr)
    return 1

  now = datetime.now(timezone.utc)
  if args.calendar:
    payload = build_kalshi_portfolio_pnl_report(kalshi, now=now)
  else:
    closed = closed_round_trips(kalshi)
    h24 = _summarize_rolling(closed, since=now - timedelta(hours=24))
    d7 = _summarize_rolling(closed, since=now - timedelta(days=7))
    bal = kalshi.portfolio_balance() or {}
    balance_cents = kalshi.balance_cents_from_payload(bal)
    payload = {
      "ok": True,
      "generated_at": now.isoformat(),
      "balance_usd": kalshi.balance_usd_from_cents(balance_cents),
      "last_24h": h24,
      "last_7d": d7,
    }

  if args.json:
    print(json.dumps(payload, indent=2))
    return 0

  print("Kalshi realized P&L (exchange fills + settlements)")
  print(f"Generated {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
  if payload.get("balance_usd") is not None:
    print(f"Cash balance now: ${float(payload['balance_usd']):.2f}")
  print()

  if args.calendar:
    blocks = [
      (f"Today ({payload['today']['label']})", payload["today"]),
      (f"Current week ({payload['current_week']['label']})", payload["current_week"]),
    ]
  else:
    blocks = [
      ("Last 24 hours", payload["last_24h"]),
      ("Last 7 days", payload["last_7d"]),
    ]

  for label, block in blocks:
    print(f"=== {label} ===")
    print(f"Total: ${block['total_pnl_usd']:+.2f}  ({block['closed_legs']} closed legs)")
    if not block["by_category"]:
      print("  (no closed legs in window)")
    else:
      for row in block["by_category"]:
        wr = f"{row['win_rate'] * 100:.0f}%" if row.get("win_rate") is not None else "—"
        print(
          f"  {row['category']:<18} ${row['total_pnl_usd']:+7.2f}  "
          f"({row['closed_legs']} legs, {wr} WR)"
        )
    print()
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
