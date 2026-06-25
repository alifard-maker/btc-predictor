#!/usr/bin/env python3
"""Show calibration report from logged predictions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click
from rich.console import Console
from rich.table import Table

from src.calibration.tracker import CalibrationTracker
from src.config import load_config

console = Console()


@click.command()
def main() -> None:
  cfg = load_config()
  tracker = CalibrationTracker(cfg)

  summary = tracker.summary()
  if summary.get("n_resolved", 0) == 0:
    console.print("[yellow]No resolved predictions yet. Run the predictor first.[/yellow]")
    return

  console.print("\n[bold]Summary[/bold]")
  for k, v in summary.items():
    if v is not None:
      console.print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")

  report = tracker.calibration_report()
  if not report.empty:
    table = Table(title="Calibration Bins")
    for col in report.columns:
      table.add_column(col)
    for _, row in report.iterrows():
      table.add_row(*[f"{v:.4f}" if isinstance(v, float) else str(v) for v in row])
    console.print(table)


if __name__ == "__main__":
  main()
