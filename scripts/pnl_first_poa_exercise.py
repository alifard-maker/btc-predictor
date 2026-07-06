#!/usr/bin/env python3
"""Request POA live wake after owner ping timeout (requires owner_poa_live in config)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.trading.pnl_first_railway_manager import load_manager_state, manager_log_dir, save_manager_state


def main() -> int:
  parser = argparse.ArgumentParser(description="Exercise P&L-first owner POA live wake request")
  parser.add_argument("--reason", default="ping_unanswered", help="Why POA is being exercised")
  parser.add_argument("--clear", action="store_true", help="Clear poa_exercise_requested and poa_live_active")
  args = parser.parse_args()

  import yaml

  cfg_path = ROOT / "config.yaml"
  cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
  mgr = dict((cfg or {}).get("pnl_first_manager") or {})
  if not mgr.get("owner_poa_live") and not args.clear:
    print("owner_poa_live is false in config — POA not authorized", file=sys.stderr)
    return 1

  state = load_manager_state(cfg)
  now = datetime.now(timezone.utc).isoformat()
  if args.clear:
    state.pop("poa_exercise_requested", None)
    state.pop("poa_exercise_reason", None)
    state.pop("poa_exercise_at", None)
    state.pop("poa_live_active", None)
    state.pop("poa_wake_at", None)
    print("POA exercise flags cleared")
  else:
    state["poa_exercise_requested"] = True
    state["poa_exercise_reason"] = args.reason
    state["poa_exercise_at"] = now
    print(json.dumps({"poa_exercise_requested": True, "reason": args.reason, "at": now}, indent=2))

  save_manager_state(state, cfg)
  print(f"state written to {manager_log_dir(cfg) / 'manager_state.json'}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
