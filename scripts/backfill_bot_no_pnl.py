#!/usr/bin/env python3
"""Backfill inverted historical NO exit P&L in bot trade databases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.trading.bot_pnl_backfill import backfill_all_bot_dbs, backfill_bot_db


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--data-dir", help="Data root (default: parent of cfg paths.logs)")
  parser.add_argument("--db", help="Single bot database path to backfill")
  parser.add_argument("--dry-run", action="store_true", help="Report fixes without writing")
  parser.add_argument("--assets", default="btc,eth", help="Comma-separated assets")
  parser.add_argument("--kinds", default="hourly,slot15", help="Comma-separated bot kinds")
  args = parser.parse_args()

  cfg = load_config()
  data_dir = Path(args.data_dir) if args.data_dir else Path(cfg["paths"]["logs"]).parent
  assets = tuple(a.strip().lower() for a in args.assets.split(",") if a.strip())
  kinds = tuple(k.strip().lower() for k in args.kinds.split(",") if k.strip())

  if args.db:
    stats = backfill_bot_db(args.db, dry_run=args.dry_run, data_dir=data_dir, cfg=cfg)
  else:
    stats = backfill_all_bot_dbs(
      data_dir,
      assets=assets,
      kinds=kinds,
      dry_run=args.dry_run,
      cfg=cfg,
    )

  print(json.dumps(stats, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
