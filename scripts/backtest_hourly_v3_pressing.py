#!/usr/bin/env python3
"""V3 pressing-mode mechanics backtest — compare baseline A/B vs hour momentum governor."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from src.backtest.hourly_v3_pressing import (
  VARIANT_LABELS,
  run_pressing_threshold_grid,
  run_v3_pressing_comparison,
)
from src.config import load_config
from src.data.storage import CandleStorage

console = Console()


def _row(label: str, r: dict) -> list[str]:
  closed = r["wins"] + r["losses"]
  return [
    label,
    str(r["hours_with_fills"]),
    str(r["filled_enters"]),
    f"{r['win_rate']:.1%}" if closed else "—",
    f"${r.get('expectancy_per_fill_usd', 0):.4f}",
    f"${r['total_pnl_usd']:+.2f}",
    f"${r.get('max_drawdown_usd', 0):.2f}",
  ]


def main() -> None:
  p = argparse.ArgumentParser(description="V3 pressing-mode hourly mechanics backtest")
  p.add_argument("--years", type=float, default=3.0)
  p.add_argument("--holdout-frac", type=float, default=0.30)
  p.add_argument("--max-spend", type=float, default=15.0)
  p.add_argument("--grid", action="store_true", help="Run small pressing threshold grid on holdout")
  p.add_argument("--fast", action="store_true", help="Skip grid; comparison only")
  p.add_argument("--output", default="data/logs/backtest_v3_pressing.json")
  args = p.parse_args()

  cfg = load_config()
  storage = CandleStorage(cfg)
  df_1h = storage.load("1h")
  if df_1h.empty:
    console.print("[red]No 1h candles. Run: python scripts/collect_historical.py[/red]")
    sys.exit(1)

  console.print(
    f"Running V3 pressing comparison on up to {args.years}y "
    f"({len(df_1h):,} bars, holdout={args.holdout_frac:.0%})..."
  )
  result = run_v3_pressing_comparison(
    cfg,
    df_1h,
    years=args.years,
    holdout_frac=args.holdout_frac,
    max_spend=args.max_spend,
  )

  if args.grid and not args.fast:
    console.print("[dim]Running pressing threshold grid on holdout...[/dim]")
    result["threshold_grid"] = run_pressing_threshold_grid(
      cfg, df_1h, years=args.years, holdout_frac=args.holdout_frac,
    )

  result["generated_at"] = datetime.now(timezone.utc).isoformat()
  out = Path(args.output)
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text(json.dumps(result, indent=2, default=str))
  console.print(f"[green]Wrote {out}[/green]")

  for section, title in (
    ("full_period", "Full period"),
    ("in_sample", "In-sample (first 70%)"),
    ("holdout", "Holdout (last 30%)"),
  ):
    t = Table(title=f"{title} — V1 momentum μ mechanics")
    t.add_column("Variant")
    t.add_column("Hours w/ fills")
    t.add_column("Fills")
    t.add_column("Win%")
    t.add_column("$/fill")
    t.add_column("Total PnL")
    t.add_column("Max DD")
    for key, label in VARIANT_LABELS.items():
      t.add_row(*_row(label.split(" — ")[0], result[section][key]))
    console.print(t)

  deltas = result["deltas_vs_baseline_a"]
  console.print("\n[bold]PnL delta vs Baseline A[/bold]")
  for variant, d in deltas.items():
    console.print(
      f"  {variant}: full ${d['full_pnl_usd']:+.2f}, holdout ${d['holdout_pnl_usd']:+.2f}"
    )

  if result.get("threshold_grid", {}).get("best"):
    best = result["threshold_grid"]["best"]
    console.print(
      f"\n[bold]Best grid config (holdout)[/bold]: "
      f"cons={best['conservative_late_edge_cents']:.0f}¢ "
      f"press={best['pressing_late_edge_cents']:.0f}¢ "
      f"profit_protect=${best['profit_protect_pnl_usd']:.2f} → "
      f"${best['holdout_pnl_usd']:+.2f}"
    )


if __name__ == "__main__":
  main()
