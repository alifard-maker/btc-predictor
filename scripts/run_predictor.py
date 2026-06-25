#!/usr/bin/env python3
"""Run the live prediction assistant."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click
from src.config import load_config
from src.scheduler.loop import PredictionLoop, run_once


@click.command()
@click.option("--once", is_flag=True, help="Run a single prediction cycle and exit")
@click.option("--model", default=None, help="Path to trained model.joblib")
def main(once: bool, model: str | None) -> None:
  cfg = load_config()
  if once:
    run_once(cfg, model)
  else:
    PredictionLoop(cfg, model).start_blocking()


if __name__ == "__main__":
  main()
