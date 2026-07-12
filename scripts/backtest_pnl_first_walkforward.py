#!/usr/bin/env python3
"""Walk-forward ML + mechanics gap analysis and V1 ML baseline."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.backtest.hourly_mechanics_backtest import run_mechanics_backtest
from src.backtest.hourly_v1_v2_compare import run_v1_walk_forward
from src.backtest.walk_forward import WalkForwardConfig
from src.config import load_config
from src.data.storage import CandleStorage

DEFAULT_OUT = Path(os.getenv("DATA_DIR", str(ROOT / "data"))) / "logs" / "backtest_pnl_first_walkforward.json"


def main() -> int:
  import argparse

  parser = argparse.ArgumentParser(description="Walk-forward ML vs frozen mechanics comparison")
  parser.add_argument("--years", type=float, default=3.0)
  parser.add_argument("--skip-walk-forward", action="store_true", help="Skip slow ML folds")
  parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
  args = parser.parse_args()

  cfg = load_config()
  df = CandleStorage(cfg).load("1h").sort_values("timestamp").reset_index(drop=True)
  if df.empty:
    print("error: no 1h candles — run backfill_1h_candles_railway.py first", flush=True)
    return 1
  end = df["timestamp"].max()
  df = df[df["timestamp"] >= end - __import__("pandas").Timedelta(days=int(args.years * 365.25))]
  span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds() / 86400.0
  print(f"loaded {len(df):,} 1h bars spanning {span_days:.1f}d", flush=True)

  out: dict = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "years": args.years,
    "bars": len(df),
    "span_days": round(span_days, 2),
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
    wf_cfg = WalkForwardConfig.from_config(cfg)
    min_needed = wf_cfg.train_window + wf_cfg.test_window
    print(
      f"walk-forward requires >= {min_needed} clean feature rows "
      f"(train={wf_cfg.train_window}, test={wf_cfg.test_window})",
      flush=True,
    )
    print("running V1 walk-forward ML (rolling retrain)...", flush=True)
    try:
      out["v1_walk_forward_ml"] = run_v1_walk_forward(cfg, df, None, model_type="random_forest")
    except ValueError as exc:
      out["v1_walk_forward_ml"] = {
        "skipped": True,
        "reason": str(exc),
        "note": "Walk-forward failed — queue will re-run after sufficient 1h history.",
      }
      args.output.parent.mkdir(parents=True, exist_ok=True)
      args.output.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
      print(f"walk-forward FAILED: {exc}", flush=True)
      print(f"wrote {args.output}", flush=True)
      return 1
  else:
    out["v1_walk_forward_ml"] = {"skipped": True, "reason": "cli_skip_walk_forward"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return 1

  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
  print(f"wrote {args.output}", flush=True)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
