#!/usr/bin/env python3
"""Fit V2 path-hourly calibration from historical backtest data and optionally apply it."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from src.calibration.v2_calibration import (
  V2CalibrationParams,
  calibrate_v2_from_backtest,
  save_v2_calibration,
)
from src.config import load_config
from src.data.storage import CandleStorage

console = Console()


def _pct(v: float | None) -> str:
  return f"{v:.1%}" if v is not None else "—"


def main() -> None:
  p = argparse.ArgumentParser(description="Calibrate hourly V2 from backtest data")
  p.add_argument("--years", type=float, default=3.0)
  p.add_argument("--train-frac", type=float, default=0.70)
  p.add_argument("--asset", default="btc", choices=["btc", "eth"])
  p.add_argument("--apply", action="store_true", help="Write calibrated params to data/logs/hourly_v2_calibration*.json")
  p.add_argument("--skip-mechanics", action="store_true", help="Skip holdout mechanics validation (faster)")
  p.add_argument("--max-train-hours", type=int, default=5000, help="Cap train sample for grid search")
  p.add_argument("--output", default="data/logs/v2_calibration_report.json")
  args = p.parse_args()

  cfg = load_config()
  storage = CandleStorage(cfg)
  tf = "1h" if args.asset == "btc" else "1h"  # eth uses same storage key pattern
  df = storage.load(tf)
  if df.empty:
    console.print("[red]No 1h candle data. Run: python scripts/collect_historical.py[/red]")
    sys.exit(1)

  if args.years > 0:
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=int(args.years * 365.25))
    df = df[df["timestamp"] >= start].reset_index(drop=True)

  console.print(
    f"Calibrating V2 ({args.asset}) on {len(df):,} hourly bars "
    f"({df['timestamp'].min()} → {df['timestamp'].max()})..."
  )

  result = calibrate_v2_from_backtest(
    cfg,
    df,
    train_frac=args.train_frac,
    validate_mechanics=not args.skip_mechanics,
    max_train_hours=args.max_train_hours,
  )
  result["generated_at"] = datetime.now(timezone.utc).isoformat()
  result["asset"] = args.asset
  result["years"] = args.years

  out = Path(args.output)
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text(json.dumps(result, indent=2, default=str))
  console.print(f"[green]Wrote report {out}[/green]")

  cal = V2CalibrationParams(**result["calibrated_params"])
  def_p = result["default_params"]

  pt = Table(title="Calibrated V2 params (vs config defaults)")
  pt.add_column("Param")
  pt.add_column("Default")
  pt.add_column("Calibrated")
  for key in ("path_weight", "structure_weight", "momentum_weight", "recovery_weight", "shock_threshold_pct", "sigma_scale"):
    pt.add_row(key, f"{def_p[key]:.4f}", f"{result['calibrated_params'][key]:.4f}")
  console.print(pt)

  ht = Table(title="Holdout forecast (intrahour polls 1–3)")
  ht.add_column("Variant")
  ht.add_column("Direction acc")
  ht.add_column("Mean |μ err|")
  ht.add_column("N")
  for label, block in (("Default", result["holdout"]["default"]), ("Calibrated", result["holdout"]["calibrated"])):
    m = block["intrahour_polls"]
    ht.add_row(label, _pct(m.get("direction_accuracy")), f"${m.get('mean_abs_error_usd', 0):.2f}", str(m.get("n", 0)))
  console.print(ht)

  if "mechanics_calibrated" in result["holdout"]:
    mt = Table(title="Holdout mechanics (current profile, v2_path μ)")
    mt.add_column("Variant")
    mt.add_column("Total PnL")
    mt.add_column("$/fill")
    mt.add_column("Fills")
    for key, label in (("mechanics_default", "Default"), ("mechanics_calibrated", "Calibrated")):
      r = result["holdout"][key]
      mt.add_row(label, f"${r['total_pnl_usd']:+.2f}", f"${r.get('expectancy_per_fill_usd', 0):.4f}", str(r["filled_enters"]))
    delta = result["holdout"].get("mechanics_delta_usd", 0)
    console.print(mt)
    console.print(f"Holdout mechanics delta (calibrated − default): [bold]${delta:+.2f}[/bold]")

  if args.apply:
    path = save_v2_calibration(cfg, cal, asset=args.asset, meta={"source": "backtest", "report": str(out)})
    console.print(f"[green]Applied calibration → {path}[/green]")
    console.print("Restart scheduler / redeploy for V2 paper bots to pick up new params.")
  else:
    console.print("Dry run only. Pass --apply to write hourly_v2_calibration.json for live V2.")


if __name__ == "__main__":
  main()
