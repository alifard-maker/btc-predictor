#!/usr/bin/env python3
"""Backfill ~3y of 1h BTC candles on Railway (/data volume) before structure sweeps."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.config import ensure_dirs, load_config
from src.data.storage import CandleStorage, HistoricalCollector

DEFAULT_MANIFEST = ROOT / "data" / "logs" / "backfill_1h_manifest.json"


def main() -> int:
  import argparse

  parser = argparse.ArgumentParser(description="Backfill 1h BTC candles for Railway backtests")
  parser.add_argument("--years", type=int, default=3)
  parser.add_argument("--force-full", action="store_true", default=True)
  parser.add_argument("--no-force-full", action="store_false", dest="force_full")
  parser.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
  args = parser.parse_args()

  cfg = load_config()
  ensure_dirs(cfg)
  storage = CandleStorage(cfg)
  before = len(storage.load("1h"))

  collector = HistoricalCollector(cfg)
  print(f"backfilling 1h candles ({args.years}y, force_full={args.force_full})...", flush=True)
  fetched = collector.collect_candles("1h", years=args.years, force_full=args.force_full)

  df = storage.load("1h").sort_values("timestamp").reset_index(drop=True)
  after = len(df)
  start_ts = df["timestamp"].iloc[0].isoformat() if not df.empty else None
  end_ts = df["timestamp"].iloc[-1].isoformat() if not df.empty else None
  span_days = (
    (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds() / 86400.0
    if len(df) > 1
    else 0.0
  )

  manifest = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "interval": "1h",
    "years_requested": args.years,
    "force_full": args.force_full,
    "bars_before": before,
    "bars_fetched": fetched,
    "bars_after": after,
    "span_days": round(span_days, 2),
    "start_ts": start_ts,
    "end_ts": end_ts,
    "parquet": str(storage.path_for("1h")),
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

  print(
    f"done: bars={after:,} span={span_days:.1f}d "
    f"({start_ts} → {end_ts}) manifest={args.output}",
    flush=True,
  )
  if span_days < 300:
    print("warning: <300d of 1h history — sweep may be underpowered", flush=True)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
