#!/usr/bin/env python3
"""Compare ETH paper (split-hold experiment) vs Kalshi live epoch P&L."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.trading.pnl_first_paper_ab import write_paper_ab_report


def main() -> int:
  cfg = load_config()
  from src.scheduler.loop import PredictionLoop

  loop = PredictionLoop(cfg)
  payload = write_paper_ab_report(loop, cfg)
  print(json.dumps(payload, indent=2), flush=True)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
