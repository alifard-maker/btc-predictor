#!/usr/bin/env python3
"""Focused 3y pnl_first variant comparison (no deploy)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.backtest.hourly_mechanics_backtest import MechanicsSimOptions, run_mechanics_backtest
from src.config import load_config
from src.data.storage import CandleStorage

console = Console()
DEFAULT_OUT = ROOT / "data" / "logs" / "backtest_pnl_first_focused.json"

VARIANTS: list[tuple[str, str, MechanicsSimOptions]] = [
  ("pnl_first_baseline", "Baseline pnl_first (mid-edge rank, simplified regime)", MechanicsSimOptions()),
  (
    "pnl_first_live_regime",
    "pnl_first + HourlyRegimeFilter (live-identical)",
    MechanicsSimOptions(use_live_regime=True),
  ),
  (
    "pnl_first_ask_edge_rank",
    "pnl_first + rank by ask-edge",
    MechanicsSimOptions(rank_by_ask_edge=True),
  ),
  (
    "pnl_first_cut_loss_0.35",
    "pnl_first + looser cut_loss ($0.35)",
    MechanicsSimOptions(cut_loss_min_usd=0.35),
  ),
  (
    "pnl_first_cut_loss_0.10",
    "pnl_first + tighter cut_loss ($0.10)",
    MechanicsSimOptions(cut_loss_min_usd=0.10),
  ),
  (
    "pnl_first_hold_settlement",
    "pnl_first + hold-to-settlement (no cut-loss)",
    MechanicsSimOptions(disable_cut_loss=True),
  ),
]


def _table(results: dict[str, dict]) -> Table:
  t = Table(title="P&L-first focused 3y backtest variants")
  t.add_column("Variant")
  t.add_column("Total PnL")
  t.add_column("$/hour")
  t.add_column("Fills")
  t.add_column("Win%")
  t.add_column("CUT LOSS $")
  for name, r in results.items():
    closed = r["wins"] + r["losses"]
    cut = (r.get("by_exit_type") or {}).get("CUT LOSSES", {})
    t.add_row(
      name,
      f"${r['total_pnl_usd']:+.2f}",
      f"${r['avg_pnl_per_hour_usd']:+.4f}",
      str(r["filled_enters"]),
      f"{r['win_rate']:.1%}" if closed else "—",
      f"${cut.get('pnl', 0):+.0f}" if cut else "—",
    )
  return t


def main() -> None:
  import argparse

  p = argparse.ArgumentParser()
  p.add_argument("--years", type=float, default=3.0)
  p.add_argument("--max-spend", type=float, default=15.0)
  p.add_argument("--output", default=str(DEFAULT_OUT))
  args = p.parse_args()

  cfg = load_config()
  storage = CandleStorage(cfg)
  df = storage.load("1h")
  if df.empty:
    console.print("[red]No 1h candle data[/red]")
    sys.exit(1)

  if args.years > 0:
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=int(args.years * 365.25))
    df = df[df["timestamp"] >= start].reset_index(drop=True)

  console.print(f"Running {len(df):,} bars ({df['timestamp'].min()} → {df['timestamp'].max()})")

  results: dict[str, dict] = {}
  labels: dict[str, str] = {}
  for key, label, opts in VARIANTS:
    console.print(f"  [cyan]{key}[/cyan]...")
    results[key] = run_mechanics_backtest(
      df, cfg, profile="pnl_first", max_spend=args.max_spend, sim_options=opts,
    )
    results[key]["variant_label"] = label
    labels[key] = label

  baseline_pnl = results["pnl_first_baseline"]["total_pnl_usd"]
  out = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "years_requested": args.years,
    "bars": len(df),
    "period_start": str(df["timestamp"].min()),
    "period_end": str(df["timestamp"].max()),
    "max_spend_per_hour_usd": args.max_spend,
    "baseline_reference": "data/logs/backtest_pnl_first_3y.json",
    "variants": results,
    "labels": labels,
    "delta_vs_baseline_usd": {
      k: round(v["total_pnl_usd"] - baseline_pnl, 2) for k, v in results.items()
    },
    "disclaimer": (
      "Synthetic hourly Kalshi books from 1h OHLC. Directional comparison only."
    ),
  }

  out_path = Path(args.output)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  out_path.write_text(json.dumps(out, indent=2))
  console.print(_table(results))
  console.print(f"\n[green]Saved {out_path}[/green]")


if __name__ == "__main__":
  main()
