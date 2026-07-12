"""Rank structure sweep variants on comparable full-horizon backtests only."""

from __future__ import annotations

from typing import Any

_MIN_FULL_HORIZON_HOURS = 20_000


def is_full_horizon_result(result: dict[str, Any] | None, *, fair: dict[str, Any] | None = None) -> bool:
  """True when a variant ran the same ~3y window as fair baseline (not truncated)."""
  if not result:
    return False
  hours = int(result.get("hours_simulated") or 0)
  if hours >= _MIN_FULL_HORIZON_HOURS:
    return True
  fair_hours = int((fair or {}).get("hours_simulated") or 0)
  if fair_hours >= _MIN_FULL_HORIZON_HOURS:
    return hours >= int(fair_hours * 0.95)
  return False


def full_horizon_struct_items(
  results: dict[str, Any],
  *,
  fair: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
  fair = fair or results.get("fair_baseline_gates") or {}
  items = [
    (name, row)
    for name, row in results.items()
    if str(name).startswith("struct_") and is_full_horizon_result(row, fair=fair)
  ]
  items.sort(key=lambda kv: float(kv[1].get("total_pnl_usd") or 0), reverse=True)
  return items


def best_structure_variant(
  results: dict[str, Any],
  *,
  fair: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]] | None:
  items = full_horizon_struct_items(results, fair=fair)
  return items[0] if items else None
