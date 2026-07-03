#!/usr/bin/env python3
"""Bootstrap 1h candle parquet for index assets (SPX/NDX) from yfinance."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.data.index_candle_bootstrap import bootstrap_index_candles


def bootstrap(asset: str, *, years: int = 3) -> Path:
  path = bootstrap_index_candles(asset, years=years, cfg=load_config())
  print(f"Saved 1h bars to {path}")
  return path


def main() -> None:
  parser = argparse.ArgumentParser(description="Bootstrap index 1h candles from yfinance")
  parser.add_argument("assets", nargs="*", default=["spx", "ndx"], choices=["spx", "ndx"])
  parser.add_argument("--years", type=int, default=3)
  args = parser.parse_args()
  for asset in args.assets:
    bootstrap(asset, years=args.years)


if __name__ == "__main__":
  main()
