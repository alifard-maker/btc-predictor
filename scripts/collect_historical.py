#!/usr/bin/env python3
"""Phase 1: collect historical BTC and auxiliary data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click
from rich.console import Console

from src.config import ensure_dirs, load_config
from src.data.storage import HistoricalCollector

console = Console()


@click.command()
@click.option("--years", default=None, type=int, help="Years of history (default from config)")
@click.option("--auxiliary/--no-auxiliary", default=True, help="Also fetch funding, OI, macro")
def main(years: int | None, auxiliary: bool) -> None:
  cfg = load_config()
  ensure_dirs(cfg)
  collector = HistoricalCollector(cfg)

  console.print("[bold]Collecting BTC candle data...[/bold]")
  results = collector.collect_all()
  for interval, count in results.items():
    console.print(f"  {interval}: {count:,} new candles")

  if auxiliary:
    console.print("\n[bold]Collecting auxiliary data...[/bold]")
    aux = collector.collect_auxiliary()
    for name, count in aux.items():
      status = f"{count:,} rows" if count >= 0 else "[red]failed[/red]"
      console.print(f"  {name}: {status}")

  console.print("\n[green]Done.[/green]")


if __name__ == "__main__":
  main()
