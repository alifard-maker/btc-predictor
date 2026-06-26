#!/usr/bin/env python3
"""Backfill prediction history with Kalshi KXBTC15M BRTI open/close prices."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.calibration.backfill import backfill_kalshi_predictions
from src.config import load_config


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dry-run", action="store_true", help="Report counts without writing")
  parser.add_argument("--limit", type=int, default=None, help="Only process the last N rows")
  args = parser.parse_args()

  stats = backfill_kalshi_predictions(load_config(), dry_run=args.dry_run, limit=args.limit)
  print("Kalshi prediction backfill:")
  for key, val in stats.items():
    print(f"  {key}: {val}")


if __name__ == "__main__":
  main()
