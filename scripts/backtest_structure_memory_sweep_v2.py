#!/usr/bin/env python3
"""Phase A: fair baselines + structure-memory parameter grid (3y synthetic)."""

from __future__ import annotations

import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.backtest.hourly_mechanics_backtest import MechanicsSimOptions, run_mechanics_backtest
from src.config import load_config
from src.data.storage import CandleStorage

DEFAULT_OUT = ROOT / "data" / "logs" / "backtest_structure_memory_sweep_v2.json"

GATES = MechanicsSimOptions(rank_by_ask_edge=True, use_live_regime=True)


def _row(name: str, r: dict) -> dict:
  return {
    "name": name,
    "total_pnl_usd": r["total_pnl_usd"],
    "filled_enters": r["filled_enters"],
    "expectancy_per_fill_usd": r["expectancy_per_fill_usd"],
    "win_rate": r["win_rate"],
    "hours_with_fills": r["hours_with_fills"],
    "cut_loss_pnl": (r.get("by_exit_type") or {}).get("CUT LOSSES", {}).get("pnl"),
  }


def main() -> int:
  import argparse

  parser = argparse.ArgumentParser(description="Structure-memory sweep v2 with fair baselines")
  parser.add_argument("--years", type=float, default=3.0)
  parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
  parser.add_argument("--quick", action="store_true", help="Smaller grid for smoke test")
  args = parser.parse_args()

  cfg = load_config()
  df = CandleStorage(cfg).load("1h").sort_values("timestamp").reset_index(drop=True)
  end = df["timestamp"].max()
  df = df[df["timestamp"] >= end - pd.Timedelta(days=int(args.years * 365.25))]

  results: dict[str, dict] = {}
  summary_rows: list[dict] = []

  print("baseline pnl_first (frozen)...", flush=True)
  results["baseline_frozen"] = run_mechanics_backtest(df, cfg, profile="pnl_first", max_spend=15.0)
  summary_rows.append(_row("baseline_frozen", results["baseline_frozen"]))

  print("fair baseline ask_edge + live_regime...", flush=True)
  results["fair_baseline_gates"] = run_mechanics_backtest(
    df, cfg, profile="pnl_first", max_spend=15.0, sim_options=GATES,
  )
  summary_rows.append(_row("fair_baseline_gates", results["fair_baseline_gates"]))

  lookbacks = [6, 12] if args.quick else [6, 12, 18]
  mu_pulls = [0.15, 0.25] if args.quick else [0.15, 0.25, 0.35]
  upper_boxes = [0.75, 0.85]

  for lb, pull, ubox in itertools.product(lookbacks, mu_pulls, upper_boxes):
    name = f"struct_lb{lb}_pull{pull}_ub{ubox}"
    print(f"running {name}...", flush=True)
    opt = MechanicsSimOptions(
      structure_memory=True,
      structure_lookback_bars=lb,
      structure_mu_pull_strength=pull,
      structure_upper_box_fraction=ubox,
      rank_by_ask_edge=True,
      use_live_regime=True,
    )
    results[name] = run_mechanics_backtest(df, cfg, profile="pnl_first", max_spend=15.0, sim_options=opt)
    summary_rows.append(_row(name, results[name]))

  fair_pnl = float(results["fair_baseline_gates"]["total_pnl_usd"])
  best = max(
    ((k, v) for k, v in results.items() if k.startswith("struct_")),
    key=lambda kv: kv[1]["total_pnl_usd"],
  )

  payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "years": args.years,
    "fair_baseline_pnl_usd": fair_pnl,
    "best_structure": {"name": best[0], **_row(best[0], best[1])},
    "delta_vs_fair_usd": round(float(best[1]["total_pnl_usd"]) - fair_pnl, 2),
    "summary": sorted(summary_rows, key=lambda r: r["total_pnl_usd"], reverse=True),
    "results": results,
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
  print(f"wrote {args.output}", flush=True)
  print(
    f"fair=${fair_pnl:,.2f} best={best[0]} ${best[1]['total_pnl_usd']:,.2f} "
    f"delta=${payload['delta_vs_fair_usd']:+,.2f}",
    flush=True,
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
