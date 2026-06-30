#!/usr/bin/env python3
"""Void phantom period-settlement exits logged before real market settle time."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.config import load_config
from src.trading.bot_phantom_settlement_cleanup import cleanup_all_phantom_settlement_dbs


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--data-dir", type=Path, default=None)
  args = parser.parse_args()

  cfg = load_config()
  data_dir = args.data_dir or Path(cfg["paths"]["logs"]).parent
  stats = cleanup_all_phantom_settlement_dbs(data_dir, dry_run=args.dry_run, cfg=cfg)
  print(json.dumps(stats, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
