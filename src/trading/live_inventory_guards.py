"""Live-mode inventory caps to prevent correlated position sprawl on Kalshi."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.trading.entry_strategy import EntryStrategyConfig

_DEFAULTS: dict[str, Any] = {
  "enabled": True,
  "force_correlation_guard": True,
  "correlation_min_strike_gap_pct": 0.18,
  "max_same_side_threshold_legs": 1,
  "max_same_side_range_legs": 1,
  "max_concurrent_positions": 4,
  "max_entries_per_cycle": 2,
  "allow_scale_in": False,
}


def _live_inventory_cfg(cfg: dict[str, Any] | None, *, kind: str) -> dict[str, Any]:
  if not cfg:
    return {}
  if kind == "slot15":
    bot_cfg = (cfg.get("intra_slot") or {}).get("bot") or {}
  else:
    bot_cfg = (cfg.get("hourly") or {}).get("bot") or {}
  return dict(bot_cfg.get("live_inventory") or {})


def apply_live_inventory_guards(
  estrat: EntryStrategyConfig,
  cfg: dict[str, Any] | None,
  *,
  mode: str,
  kind: str = "hourly",
) -> EntryStrategyConfig:
  """Tighten entry strategy in live mode to limit correlated inventory sprawl."""
  if mode != "live":
    return estrat
  from src.trading.hourly_live_trial_align import skip_live_inventory_guards

  if skip_live_inventory_guards(cfg, kind=kind, mode=mode):
    return estrat
  inv = _live_inventory_cfg(cfg, kind=kind)
  if not inv.get("enabled", _DEFAULTS["enabled"]):
    return estrat

  kw: dict[str, Any] = {}
  if inv.get("force_correlation_guard", _DEFAULTS["force_correlation_guard"]):
    kw["correlation_guard"] = True
  kw["correlation_min_strike_gap_pct"] = float(
    inv.get("correlation_min_strike_gap_pct", _DEFAULTS["correlation_min_strike_gap_pct"])
  )
  kw["max_same_side_threshold_legs"] = int(
    inv.get("max_same_side_threshold_legs", _DEFAULTS["max_same_side_threshold_legs"])
  )
  kw["max_same_side_range_legs"] = int(
    inv.get("max_same_side_range_legs", _DEFAULTS["max_same_side_range_legs"])
  )
  max_concurrent = int(inv.get("max_concurrent_positions", _DEFAULTS["max_concurrent_positions"]))
  kw["max_concurrent_positions"] = min(estrat.max_concurrent_positions, max_concurrent)
  max_entries = int(inv.get("max_entries_per_cycle", _DEFAULTS["max_entries_per_cycle"]))
  kw["max_entries_per_cycle"] = min(estrat.max_entries_per_cycle, max_entries)
  if "allow_scale_in" in inv:
    kw["allow_scale_in"] = bool(inv["allow_scale_in"])
  else:
    kw["allow_scale_in"] = _DEFAULTS["allow_scale_in"]
  return replace(estrat, **kw)
