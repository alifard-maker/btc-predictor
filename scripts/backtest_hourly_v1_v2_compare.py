#!/usr/bin/env python3
"""Backtest hourly V2 path memory vs V1 baselines (walk-forward ML + momentum mechanics)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from src.backtest.hourly_v1_v2_compare import run_full_comparison
from src.config import load_config
from src.data.storage import CandleStorage

console = Console()


def _metrics_row(label: str, m: dict) -> list[str]:
  return [
    label,
    str(m.get("n_trades", m.get("n_hours", "—"))),
    f"{m.get('fill_rate', 0):.1%}" if m.get("fill_rate") is not None else "—",
    f"{m.get('win_rate', 0):.1%}" if m.get("win_rate") is not None else "—",
    f"${m.get('expectancy_usd', 0):.4f}",
    f"${m.get('total_pnl_usd', 0):.2f}",
  ]


def main() -> None:
  p = argparse.ArgumentParser(description="Compare hourly V1 vs V2 backtests")
  p.add_argument("--years", type=float, default=3.0)
  p.add_argument("--model-type", default="random_forest", help="V1 walk-forward model (lightgbm if libomp installed)")
  p.add_argument("--skip-walk-forward", action="store_true", help="Skip slow ML walk-forward fold loop")
  p.add_argument("--output", default="data/logs/backtest_v1_v2_compare.json")
  args = p.parse_args()

  cfg = load_config()
  storage = CandleStorage(cfg)
  df_1h = storage.load("1h")
  df_15m = storage.load("15m")
  if df_1h.empty:
    console.print("[red]No 1h candles. Run: python scripts/collect_historical.py[/red]")
    sys.exit(1)

  console.print(f"Running V1 vs V2 comparison on up to {args.years}y of {len(df_1h):,} hourly bars...")
  result = run_full_comparison(
    cfg,
    df_1h,
    df_15m if not df_15m.empty else None,
    years=args.years,
    model_type=args.model_type,
    include_walk_forward=not args.skip_walk_forward,
  )
  result["generated_at"] = datetime.now(timezone.utc).isoformat()
  result["model_type"] = args.model_type

  out = Path(args.output)
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text(json.dumps(result, indent=2, default=str))
  console.print(f"[green]Wrote {out}[/green]")

  t = Table(title="Signal backtest (passive fills, same edge threshold)")
  t.add_column("Variant")
  t.add_column("Hours/signals")
  t.add_column("Fill rate")
  t.add_column("Win rate")
  t.add_column("$/trade")
  t.add_column("Total PnL")
  if "v1_walk_forward" in result:
    t.add_row(*_metrics_row("V1 ML walk-forward", result["v1_walk_forward"]["metrics"]))
  t.add_row(*_metrics_row("V2 path signal", result["v2_signal"]["metrics"]))
  console.print(t)

  mt = Table(title="Mechanics replay (current deploy profile, 3y synthetic Kalshi)")
  mt.add_column("μ source")
  mt.add_column("Total PnL")
  mt.add_column("$/hour")
  mt.add_column("Fills")
  mt.add_column("Win%")
  mt.add_column("$/fill")
  for key, label in (("v1_momentum", "V1 momentum μ"), ("v2_path", "V2 path μ")):
    r = result["mechanics"][key]
    closed = r["wins"] + r["losses"]
    mt.add_row(
      label,
      f"${r['total_pnl_usd']:+.2f}",
      f"${r['avg_pnl_per_hour_usd']:+.4f}",
      str(r["filled_enters"]),
      f"{r['win_rate']:.1%}" if closed else "—",
      f"${r.get('expectancy_per_fill_usd', 0):.4f}",
    )
  console.print(mt)
  delta = result["mechanics"]["v2_minus_v1_pnl_usd"]
  console.print(f"V2 path μ vs momentum μ PnL delta: [bold]${delta:+.2f}[/bold]")

  fc = result["forecast"]
  ft = Table(title="Forecast at lock (ref=open → settle=close)")
  ft.add_column("Model")
  ft.add_column("Direction acc")
  ft.add_column("Mean |μ err|")
  ft.add_column("N")
  for key, label in (("v1_momentum", "V1 momentum"), ("v2_path_lock", "V2 path @ lock")):
    s = fc[key]
    ft.add_row(
      label,
      f"{s.get('direction_accuracy', 0):.1%}" if s.get("direction_accuracy") is not None else "—",
      f"${s.get('mean_abs_error_usd', 0):.2f}" if s.get("mean_abs_error_usd") is not None else "—",
      str(s.get("n_resolved", 0)),
    )
  console.print(ft)


if __name__ == "__main__":
  main()
