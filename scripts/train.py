#!/usr/bin/env python3
"""Train model on historical candle data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click
from rich.console import Console

from src.config import ensure_dirs, load_config
from src.data.storage import CandleStorage
from src.models.trainer import ModelTrainer

console = Console()


@click.command()
@click.option("--model-type", default=None, type=click.Choice(["lightgbm", "xgboost", "random_forest"]))
@click.option("--cv", is_flag=True, help="Run time-series cross-validation")
def main(model_type: str | None, cv: bool) -> None:
  cfg = load_config()
  ensure_dirs(cfg)

  if model_type:
    cfg["model"]["type"] = model_type

  storage = CandleStorage(cfg)
  df_1m = storage.load("1m")
  df_15m = storage.load("15m")

  if df_1m.empty:
    console.print("[red]No 1m candle data. Run collect_historical.py first.[/red]")
    sys.exit(1)

  console.print(f"Training on {len(df_1m):,} 1m candles ({cfg['model']['type']})...")
  trainer = ModelTrainer(cfg)

  if cv:
    cv_results = trainer.cross_validate(df_1m, df_15m if not df_15m.empty else None)
    console.print(f"CV AUC: {cv_results['cv_auc_mean']:.4f} ± {cv_results['cv_auc_std']:.4f}")

  metrics = trainer.train(df_1m, df_15m if not df_15m.empty else None)
  for k, v in metrics.items():
    console.print(f"  {k}: {v}")

  model_path = Path(cfg["paths"]["models"]) / "model.joblib"
  trainer.save(model_path)
  console.print(f"\n[green]Model saved to {model_path}[/green]")


if __name__ == "__main__":
  main()
