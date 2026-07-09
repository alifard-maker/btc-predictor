#!/usr/bin/env python3
"""3y backtest: mid-hour entry window + deferred paper exits vs pnl_first baseline."""

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
DEFAULT_OUT = ROOT / "data" / "logs" / "backtest_pnl_first_midhour_exits.json"

MID_HOUR_MIN = 0.25
MID_HOUR_MAX = 0.75

VARIANTS: list[tuple[str, str, MechanicsSimOptions]] = [
  (
    "pnl_first_fair_baseline",
    "Current pnl_first profile (no mid-hour gate, no defer exits)",
    MechanicsSimOptions(),
  ),
  (
    "pnl_first_mid_hour_entry",
    "Entries only when 15–45m to settle (blocks early-hour bleed bucket)",
    MechanicsSimOptions(
      entry_min_hours_to_settle=MID_HOUR_MIN,
      entry_max_hours_to_settle=MID_HOUR_MAX,
    ),
  ),
  (
    "pnl_first_mid_hour_defer_exits",
    "Mid-hour entry + defer profit target & cut losses when >30m to settle (paper policy)",
    MechanicsSimOptions(
      entry_min_hours_to_settle=MID_HOUR_MIN,
      entry_max_hours_to_settle=MID_HOUR_MAX,
      defer_exits_paper=True,
    ),
  ),
]


def _table(results: dict[str, dict]) -> Table:
  t = Table(title="P&L-first mid-hour + exit policy (3y)")
  t.add_column("Variant")
  t.add_column("Total PnL")
  t.add_column("$/fill")
  t.add_column("Fills")
  t.add_column("Win%")
  t.add_column("CUT $")
  t.add_column("TP $")
  for name, r in results.items():
    closed = r["wins"] + r["losses"]
    cut = (r.get("by_exit_type") or {}).get("CUT LOSSES", {})
    tp = (r.get("by_exit_type") or {}).get("TAKE PROFIT", {})
    fills = int(r.get("filled_enters") or 0)
    pnl = float(r.get("total_pnl_usd") or 0)
    t.add_row(
      name,
      f"${pnl:+.2f}",
      f"${pnl / fills:+.4f}" if fills else "—",
      str(fills),
      f"{r['win_rate']:.1%}" if closed else "—",
      f"${cut.get('pnl', 0):+.0f}" if cut else "—",
      f"${tp.get('pnl', 0):+.0f}" if tp else "—",
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

  baseline_pnl = results["pnl_first_fair_baseline"]["total_pnl_usd"]
  baseline_fills = int(results["pnl_first_fair_baseline"].get("filled_enters") or 0)
  out = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "years_requested": args.years,
    "bars": len(df),
    "period_start": str(df["timestamp"].min()),
    "period_end": str(df["timestamp"].max()),
    "max_spend_per_hour_usd": args.max_spend,
    "mid_hour_window_hours": {"min": MID_HOUR_MIN, "max": MID_HOUR_MAX},
    "defer_exits_minutes_to_settle": (cfg.get("pnl_first") or {}).get(
      "defer_leg_stop_minutes_to_settle"
    ),
    "variants": results,
    "labels": labels,
    "delta_vs_baseline_usd": {
      k: round(v["total_pnl_usd"] - baseline_pnl, 2) for k, v in results.items()
    },
    "delta_per_fill_vs_baseline_usd": {
      k: round(
        (v["total_pnl_usd"] / max(1, int(v.get("filled_enters") or 0)))
        - (baseline_pnl / max(1, baseline_fills)),
        4,
      )
      for k, v in results.items()
    },
    "kalshi_live_context": {
      "note": "Live epoch: 15–30m entry bucket +$0.24/trade; 45–60m bucket −$0.24/trade",
      "recommendation": "Block entries outside 15–45m; defer early cuts/targets in paper",
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
