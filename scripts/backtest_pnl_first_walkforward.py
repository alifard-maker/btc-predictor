#!/usr/bin/env python3
"""Walk-forward ML + mechanics gap analysis and V1 ML baseline.

Current mechanics backtests (hourly_mechanics_backtest) use frozen rules — no daily
retrain, no isotonic recalibration. Live runs HourlyPredictor with rolling ML + structure.

This script runs:
  1. V1 walk-forward ML backtest (rolling train/retrain per fold)
  2. pnl_first mechanics baseline (frozen rules)
  3. Documents delta — full live-fidelity backtest needs unified walk-forward mechanics
     engine (train → predict μ → gates → fills → leg stops) per hour.

Next: wire HourlyPredictor.predict() inside simulate_hour using only past bars (no lookahead).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.backtest.hourly_mechanics_backtest import run_mechanics_backtest
from src.backtest.hourly_v1_v2_compare import run_v1_walk_forward
from src.config import load_config
from src.data.storage import CandleStorage

DEFAULT_OUT = ROOT / "data" / "logs" / "backtest_pnl_first_walkforward.json"


def main() -> int:
  import argparse

  parser = argparse.ArgumentParser(description="Walk-forward ML vs frozen mechanics comparison")
  parser.add_argument("--years", type=float, default=3.0)
  parser.add_argument("--skip-walk-forward", action="store_true", help="Skip slow ML folds")
  parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
  args = parser.parse_args()

  cfg = load_config()
  df = CandleStorage(cfg).load("1h").sort_values("timestamp").reset_index(drop=True)
  end = df["timestamp"].max()
  df = df[df["timestamp"] >= end - __import__("pandas").Timedelta(days=int(args.years * 365.25))]

  out: dict = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "years": args.years,
    "gap": (
      "Mechanics backtest does not retrain ML on schedule. Walk-forward does retrain but "
      "uses simplified fills, not full pnl_first gates/leg stops. Unified engine = Phase 0b."
    ),
  }

  print("running pnl_first mechanics (frozen rules)...", flush=True)
  out["pnl_first_mechanics_frozen"] = run_mechanics_backtest(
    df, cfg, profile="pnl_first", max_spend=15.0,
  )

  if not args.skip_walk_forward:
    print("running V1 walk-forward ML (rolling retrain)...", flush=True)
    try:
      out["v1_walk_forward_ml"] = run_v1_walk_forward(cfg, df, None, model_type="random_forest")
    except ValueError as exc:
      out["v1_walk_forward_ml"] = {
        "skipped": True,
        "reason": str(exc),
        "note": (
          "Interim job: mechanics baseline only. Walk-forward needs more 1h history "
          "than Railway currently holds (or smaller train_window). Unified WF mechanics engine TBD."
        ),
      }
      print(f"walk-forward skipped: {exc}", flush=True)
  else:
    out["v1_walk_forward_ml"] = {"skipped": True}

  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
  print(f"wrote {args.output}", flush=True)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
