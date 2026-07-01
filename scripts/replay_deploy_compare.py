#!/usr/bin/env python3
"""Compare legacy vs current deployment mechanics over settled hourly events."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from scripts.replay_hourly_event import compute_trade_stats, replay_event
from src.backtest.mechanics_profiles import PROFILE_LABELS, MechanicsProfile
from src.config import load_config
from src.data.kalshi import KalshiClient
from src.data.storage import CandleStorage
from src.trading.hourly_event_time import hourly_event_settle_utc

ET = ZoneInfo("America/New_York")


def _iter_hourly_event_tickers(
  start_utc: datetime,
  end_utc: datetime,
  *,
  series: str = "KXBTCD",
) -> list[str]:
  out: list[str] = []
  cursor = start_utc.astimezone(ET).replace(minute=0, second=0, microsecond=0)
  end_et = end_utc.astimezone(ET)
  while cursor < end_et:
    suffix = cursor.strftime("%y%b%d%H").upper()
    out.append(f"{series}-{suffix}")
    cursor += timedelta(hours=1)
  return out


def _event_settled(kalshi: KalshiClient, event_ticker: str) -> bool:
  try:
    mkts = kalshi.get("/markets", params={"event_ticker": event_ticker, "limit": 5}).get("markets") or []
    if not mkts:
      return False
    return mkts[0].get("expiration_value") is not None
  except Exception:
    return False


def _aggregate_hour_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
  if not rows:
    return {
      "hours": 0,
      "filled_enters": 0,
      "resting_enters": 0,
      "exits": 0,
      "wins": 0,
      "losses": 0,
      "flats": 0,
      "win_rate": 0.0,
      "total_pnl_usd": 0.0,
      "avg_pnl_per_hour_usd": 0.0,
      "winning_hours": 0,
      "losing_hours": 0,
      "flat_hours": 0,
      "by_exit_type": {},
    }
  filled = sum(r["trade_stats"]["filled_enters"] for r in rows)
  resting = sum(r["trade_stats"]["resting_enters"] for r in rows)
  exits = sum(r["trade_stats"]["exits"] for r in rows)
  wins = sum(r["trade_stats"]["wins"] for r in rows)
  losses = sum(r["trade_stats"]["losses"] for r in rows)
  flats = sum(r["trade_stats"]["flats"] for r in rows)
  closed = wins + losses + flats
  total_pnl = round(sum(r["realized_pnl_usd"] for r in rows), 2)
  by_reason: dict[str, dict[str, float | int]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
  for row in rows:
    for k, v in (row.get("summary_by_reason") or {}).items():
      by_reason[k]["n"] += int(v["n"])
      by_reason[k]["pnl"] += float(v["pnl"])
  for k in by_reason:
    by_reason[k]["pnl"] = round(by_reason[k]["pnl"], 2)
  win_hours = sum(1 for r in rows if r["realized_pnl_usd"] > 0)
  lose_hours = sum(1 for r in rows if r["realized_pnl_usd"] < 0)
  flat_hours = sum(1 for r in rows if r["realized_pnl_usd"] == 0)
  return {
    "hours": len(rows),
    "filled_enters": filled,
    "resting_enters": resting,
    "exits": exits,
    "wins": wins,
    "losses": losses,
    "flats": flats,
    "win_rate": round(wins / closed, 4) if closed else 0.0,
    "total_pnl_usd": total_pnl,
    "avg_pnl_per_hour_usd": round(total_pnl / len(rows), 2),
    "winning_hours": win_hours,
    "losing_hours": lose_hours,
    "flat_hours": flat_hours,
    "by_exit_type": dict(by_reason),
  }


def run_compare(
  *,
  days: int = 10,
  max_spend: float = 15.0,
  profiles: list[MechanicsProfile] | None = None,
  output: Path | None = None,
) -> dict[str, Any]:
  cfg = load_config()
  profiles = profiles or ["legacy", "current"]
  storage = CandleStorage(cfg)
  df = storage.load("1m")
  df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
  candle_start = df["ts"].min().to_pydatetime()
  candle_end = df["ts"].max().to_pydatetime()

  now = datetime.now(timezone.utc)
  requested_start = now - timedelta(days=days)
  start_utc = max(requested_start, candle_start + timedelta(hours=1))
  end_utc = min(now, candle_end)

  kalshi = KalshiClient(cfg)
  tickers = _iter_hourly_event_tickers(start_utc, end_utc)
  settled: list[str] = []
  for ev in tickers:
    if _event_settled(kalshi, ev):
      settled.append(ev)

  results: dict[str, list[dict[str, Any]]] = {p: [] for p in profiles}
  errors: list[dict[str, str]] = []

  for ev in settled:
    for profile in profiles:
      try:
        r = replay_event(
          event_ticker=ev,
          cfg=cfg,
          prediction={},
          passive=True,
          max_spend=max_spend,
          mechanics_profile=profile,
        )
        if r.get("brti_path", {}).get("settle") is None:
          continue
        results[profile].append(r)
        print(f"OK {profile:16} {ev} pnl={r['realized_pnl_usd']:+.2f} fills={r['trade_stats']['filled_enters']}")
      except Exception as exc:
        errors.append({"event": ev, "profile": profile, "error": str(exc)})
        print(f"ERR {profile:16} {ev}: {exc}")

  summary = {
    profile: {
      "label": PROFILE_LABELS[profile],
      "aggregate": _aggregate_hour_rows(results[profile]),
      "hours": [
        {
          "event_ticker": r["event_ticker"],
          "settle_utc": r["settle_utc"],
          "pnl_usd": r["realized_pnl_usd"],
          "trade_stats": r["trade_stats"],
          "summary_by_reason": r.get("summary_by_reason"),
        }
        for r in results[profile]
      ],
    }
    for profile in profiles
  }

  out = {
    "generated_at": now.isoformat(),
    "requested_days": days,
    "candle_range_utc": {
      "start": candle_start.isoformat(),
      "end": candle_end.isoformat(),
    },
    "replay_window_utc": {
      "start": start_utc.isoformat(),
      "end": end_utc.isoformat(),
      "note": "Clamped to available 1m BRTI candles; may be fewer than requested days.",
    },
    "events_scanned": len(tickers),
    "events_settled_replayed": len(settled),
    "max_spend_per_hour_usd": max_spend,
    "profiles": summary,
    "comparison": {
      "pnl_delta_current_minus_legacy": round(
        summary.get("current", {}).get("aggregate", {}).get("total_pnl_usd", 0)
        - summary.get("legacy", {}).get("aggregate", {}).get("total_pnl_usd", 0),
        2,
      )
      if "legacy" in summary and "current" in summary
      else None,
    },
    "errors": errors,
    "disclaimer": (
      "Synthetic contract mids from 1m BRTI candles — directional estimate only, "
      "not exact Kalshi historical fills."
    ),
  }

  out_path = output or Path(cfg.get("paths", {}).get("logs", "data/logs")) / "replay_deploy_compare_10d.json"
  out_path.parent.mkdir(parents=True, exist_ok=True)
  out_path.write_text(json.dumps(out, indent=2))
  print(f"\nWrote {out_path}")
  return out


def main() -> None:
  p = argparse.ArgumentParser(description="Replay legacy vs current deploy mechanics")
  p.add_argument("--days", type=int, default=10)
  p.add_argument("--max-spend", type=float, default=15.0)
  p.add_argument("--output", default=None)
  p.add_argument(
    "--profiles",
    default="legacy,current",
    help="Comma-separated: legacy, mechanical_fixes, current",
  )
  args = p.parse_args()
  profiles = [x.strip() for x in args.profiles.split(",") if x.strip()]  # type: ignore[misc]
  result = run_compare(
    days=args.days,
    max_spend=args.max_spend,
    profiles=profiles,  # type: ignore[arg-type]
    output=Path(args.output) if args.output else None,
  )
  for name, block in result["profiles"].items():
    agg = block["aggregate"]
    print(
      f"\n{name} ({block['label']}): "
      f"pnl=${agg['total_pnl_usd']:+.2f} over {agg['hours']}h | "
      f"fills={agg['filled_enters']} resting={agg['resting_enters']} | "
      f"exits W/L/F={agg['wins']}/{agg['losses']}/{agg['flats']} "
      f"win_rate={agg['win_rate']:.1%}"
    )


if __name__ == "__main__":
  main()
