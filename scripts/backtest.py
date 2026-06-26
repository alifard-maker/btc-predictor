#!/usr/bin/env python3
"""Stage 2: walk-forward backtest with fee-adjusted signals."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click
from rich.console import Console
from rich.table import Table

from src.config import load_config
from src.data.storage import CandleStorage
from src.trading.backtest import Backtester

console = Console()


@click.command()
@click.option("--train-window", default=2000, help="Rolling training window (15m bars)")
@click.option("--step", default=200, help="Test step size per fold")
@click.option("--output", default=None, help="Save results CSV path")
def main(train_window: int, step: int, output: str | None) -> None:
  cfg = load_config()
  storage = CandleStorage(cfg)
  df_1m = storage.load("1m")
  df_15m = storage.load("15m")

  if df_15m.empty:
    console.print("[red]No 15m data. Run collect_historical.py first.[/red]")
    sys.exit(1)

  console.print(f"Backtesting on {len(df_15m):,} 15m candles...")
  bt = Backtester(cfg)
  results = bt.run(df_15m, df_1m if not df_1m.empty else None, train_window, step)
  analysis = bt.analyze(results)

  table = Table(title="Backtest Results")
  table.add_column("Metric")
  table.add_column("Value")
  for k, v in analysis.items():
    if v is not None:
      display = f"{v:.4f}" if isinstance(v, float) else str(v)
      table.add_row(k, display)
  console.print(table)

  if output:
    results.to_csv(output, index=False)
    console.print(f"Results saved to {output}")


if __name__ == "__main__":
  main()
