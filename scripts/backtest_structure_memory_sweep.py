#!/usr/bin/env python3
"""Sweep structure-memory lookback windows for pnl_first (iterative memory tuning)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.backtest.hourly_mechanics_backtest import MechanicsSimOptions, run_mechanics_backtest
from src.config import load_config
from src.data.storage import CandleStorage

DEFAULT_OUT = ROOT / "data" / "logs" / "backtest_structure_memory_sweep.json"


def main() -> int:
  import argparse

  parser = argparse.ArgumentParser(description="Structure-memory lookback sweep for pnl_first")
  parser.add_argument("--years", type=float, default=3.0)
  parser.add_argument("--lookbacks", default="4,6,8,12,18,24", help="Comma-separated 1h bar lookbacks")
  parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
  args = parser.parse_args()

  cfg = load_config()
  df = CandleStorage(cfg).load("1h").sort_values("timestamp").reset_index(drop=True)
  end = df["timestamp"].max()
  df = df[df["timestamp"] >= end - __import__("pandas").Timedelta(days=int(args.years * 365.25))]

  lookbacks = [int(x.strip()) for x in str(args.lookbacks).split(",") if x.strip()]
  results: dict[str, dict] = {}

  baseline = run_mechanics_backtest(df, cfg, profile="pnl_first", max_spend=15.0)
  results["pnl_first_baseline"] = baseline

  for lb in lookbacks:
    key = f"structure_memory_{lb}h"
    print(f"running {key}...", flush=True)
    results[key] = run_mechanics_backtest(
      df,
      cfg,
      profile="pnl_first",
      max_spend=15.0,
      sim_options=MechanicsSimOptions(
        structure_memory=True,
        structure_lookback_bars=lb,
        rank_by_ask_edge=True,
        use_live_regime=True,
      ),
    )

  args.output.parent.mkdir(parents=True, exist_ok=True)
  payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "years": args.years,
    "lookbacks": lookbacks,
    "note": (
      "Structure memory adjusts μ/σ from consolidation + resistance; blocks YES-above "
      "in upper box. Still rule-based (no ML retrain) — see backtest_pnl_first_walkforward.py."
    ),
    "results": results,
  }
  args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
  print(f"wrote {args.output}", flush=True)

  best = max(
    ((k, v) for k, v in results.items() if k != "pnl_first_baseline"),
    key=lambda kv: kv[1]["total_pnl_usd"],
    default=(None, baseline),
  )
  print(
    f"baseline ${baseline['total_pnl_usd']:,.2f} | best {best[0]} ${best[1]['total_pnl_usd']:,.2f}",
    flush=True,
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
