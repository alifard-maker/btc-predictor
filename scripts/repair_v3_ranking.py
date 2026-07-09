#!/usr/bin/env python3
"""Repair v3 sweep JSON best_structure to use full-horizon ranking only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.trading.structure_sweep_ranking import best_structure_variant, is_full_horizon_result


def _row(name: str, result: dict) -> dict:
  return {
    "name": name,
    "total_pnl_usd": result.get("total_pnl_usd"),
    "filled_enters": result.get("filled_enters"),
    "expectancy_per_fill_usd": result.get("expectancy_per_fill_usd"),
    "win_rate": result.get("win_rate"),
    "hours_with_fills": result.get("hours_with_fills"),
    "hours_simulated": result.get("hours_simulated"),
  }


def main() -> int:
  import os

  base = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
  path = base / "logs" / "backtest_structure_memory_sweep_v3.json"
  if not path.exists():
    print(f"missing {path}", flush=True)
    return 1
  payload = json.loads(path.read_text(encoding="utf-8"))
  results = payload.get("results") or {}
  fair = results.get("fair_baseline_gates") or {}
  fair_pnl = float(fair.get("total_pnl_usd") or 0)
  old_best = (payload.get("best_structure") or {}).get("name")
  pair = best_structure_variant(results, fair=fair)
  if not pair:
    print("no full-horizon variants", flush=True)
    return 1
  name, row = pair
  payload["best_structure"] = _row(name, row)
  payload["best_structure_full_horizon_only"] = True
  payload["delta_vs_fair_usd"] = round(float(row.get("total_pnl_usd") or 0) - fair_pnl, 2)
  if old_best and not is_full_horizon_result(results.get(old_best) or {}, fair=fair):
    payload["best_structure_repaired_from"] = old_best
  path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
  print(f"repaired best {name} delta={payload['delta_vs_fair_usd']}", flush=True)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
