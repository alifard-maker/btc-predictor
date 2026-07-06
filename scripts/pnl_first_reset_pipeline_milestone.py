#!/usr/bin/env python3
"""Reset P&L-first pipeline milestone streak (e.g. after model profile change)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.trading.pnl_first_pipeline_milestone import reset_pipeline_milestone


def main() -> int:
  parser = argparse.ArgumentParser(description="Reset pipeline milestone to 0/20")
  parser.add_argument("--reason", default="model_profile_change", help="Audit reason stored in manager state")
  args = parser.parse_args()
  cfg = load_config()
  out = reset_pipeline_milestone(cfg, reason=args.reason)
  print(json.dumps(out, indent=2))
  print(f"Pipeline milestone reset: 0/{out.get('target_pipeline_hours')}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
