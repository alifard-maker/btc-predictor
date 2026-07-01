#!/usr/bin/env python3
"""Compare legacy / current / rally-only hourly mechanics over ~3y of 1h candles."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from rich.console import Console
from rich.table import Table

from src.backtest.hourly_mechanics_backtest import run_mechanics_backtest
from src.backtest.mechanics_profiles import PROFILE_LABELS, MechanicsProfile
from src.config import load_config
from src.data.storage import CandleStorage

console = Console()


def _table(results: dict[str, dict]) -> Table:
  t = Table(title="Hourly Mechanics Backtest (~3y synthetic)")
  t.add_column("Profile")
  t.add_column("Total PnL")
  t.add_column("$/hour")
  t.add_column("Fills")
  t.add_column("W/L legs")
  t.add_column("Win%")
  t.add_column("Win hrs")
  for name, r in results.items():
    closed = r["wins"] + r["losses"]
    t.add_row(
      name,
      f"${r['total_pnl_usd']:+.2f}",
      f"${r['avg_pnl_per_hour_usd']:+.4f}",
      str(r["filled_enters"]),
      f"{r['wins']}/{r['losses']}",
      f"{r['win_rate']:.1%}" if closed else "—",
      f"{r['winning_hours']}/{r['losing_hours']}",
    )
  return t


def main() -> None:
  p = argparse.ArgumentParser()
  p.add_argument("--years", type=float, default=3.0)
  p.add_argument("--max-spend", type=float, default=15.0)
  p.add_argument(
    "--profiles",
    default="legacy,current,rally_only",
    help="legacy, mechanical_fixes, current, rally_only",
  )
  p.add_argument("--output", default="data/logs/backtest_mechanics_3y.json")
  args = p.parse_args()

  cfg = load_config()
  storage = CandleStorage(cfg)
  df = storage.load("1h")
  if df.empty:
    console.print("[red]No 1h candle data. Run: python scripts/collect_historical.py[/red]")
    sys.exit(1)

  if args.years > 0:
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=int(args.years * 365.25))
    df = df[df["timestamp"] >= start].reset_index(drop=True)

  profiles: list[MechanicsProfile] = [x.strip() for x in args.profiles.split(",") if x.strip()]  # type: ignore[misc]

  console.print(
    f"Running {len(df):,} hourly bars "
    f"({df['timestamp'].min()} → {df['timestamp'].max()})..."
  )

  results: dict[str, dict] = {}
  for profile in profiles:
    console.print(f"  [cyan]{profile}[/cyan]...")
    results[profile] = run_mechanics_backtest(
      df, cfg, profile=profile, max_spend=args.max_spend,
    )

  out = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "years_requested": args.years,
    "bars": len(df),
    "period_start": str(df["timestamp"].min()),
    "period_end": str(df["timestamp"].max()),
    "max_spend_per_hour_usd": args.max_spend,
    "profiles": results,
    "comparison": {
      "rally_minus_legacy_pnl": round(
        results.get("rally_only", {}).get("total_pnl_usd", 0)
        - results.get("legacy", {}).get("total_pnl_usd", 0),
        2,
      ),
      "rally_minus_current_pnl": round(
        results.get("rally_only", {}).get("total_pnl_usd", 0)
        - results.get("current", {}).get("total_pnl_usd", 0),
        2,
      ),
    },
    "disclaimer": (
      "Synthetic hourly Kalshi books from 1h OHLC + bot mechanics replay. "
      "Not historical Kalshi contract prices. Directional comparison across profiles."
    ),
  }

  out_path = Path(args.output)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  out_path.write_text(json.dumps(out, indent=2))
  console.print(_table(results))
  console.print(f"\n[green]Saved {out_path}[/green]")


if __name__ == "__main__":
  main()
