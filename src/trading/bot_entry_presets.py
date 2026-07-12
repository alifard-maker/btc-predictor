"""Passive vs aggressive entry presets toggled per bot via dashboard."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.trading.entry_strategy import EntryStrategyConfig, entry_strategy_from_cfg

# --- 15m passive = pre-3.13.23 production behavior ---
_SLOT15_PASSIVE_ENTRY: dict[str, Any] = {
  "kelly_fraction": 0.15,
  "max_budget_fraction_per_entry": 0.55,
  "max_entries_per_cycle": 1,
  "max_concurrent_positions": 1,
  "allow_scale_in": False,
  "scale_in_max_legs_per_ticker": 2,
  "min_ask_edge_cents": 5.0,
  "correlation_guard": False,
}

_SLOT15_AGGRESSIVE_ENTRY: dict[str, Any] = {
  "kelly_fraction": 0.45,
  "max_budget_fraction_per_entry": 0.10,
  "max_entries_per_cycle": 3,
  "max_concurrent_positions": 6,
  "allow_scale_in": True,
  "scale_in_max_legs_per_ticker": 6,
  "scale_in_min_unrealized_pnl_usd": 0.05,
  "min_ask_edge_cents": 5.0,
  "correlation_guard": False,
}

# --- Hourly passive = current hourly.bot defaults ---
_HOURLY_PASSIVE_ENTRY: dict[str, Any] = {
  "kelly_fraction": 0.15,
  "max_budget_fraction_per_entry": 0.30,
  "max_entries_per_cycle": 4,
  "max_concurrent_positions": 12,
  "max_stake_per_entry_usd": 10.0,
  "allow_scale_in": True,
  "scale_in_max_legs_per_ticker": 4,
  "scale_in_min_unrealized_pnl_usd": 0.05,
  "min_ask_edge_cents": 8.0,
  "correlation_guard": True,
  "risk_adjusted_ranking": True,
  "allow_barbell": True,
}

_HOURLY_AGGRESSIVE_ENTRY: dict[str, Any] = {
  "kelly_fraction": 0.45,
  "max_budget_fraction_per_entry": 0.10,
  "max_entries_per_cycle": 5,
  "max_concurrent_positions": 6,
  "allow_scale_in": True,
  "scale_in_max_legs_per_ticker": 8,
  "scale_in_min_unrealized_pnl_usd": 0.05,
  "min_ask_edge_cents": 5.0,
  "correlation_guard": True,
  "risk_adjusted_ranking": True,
  "allow_barbell": True,
}

_PASSIVE_RUNTIME = {
  "reentry_cooldown_seconds": 120,
  "profit_exit_cooldown_seconds": 60,
}

_AGGRESSIVE_RUNTIME = {
  "reentry_cooldown_seconds": 30,
  "profit_exit_cooldown_seconds": 30,
}


def _entry_preset_map(kind: str, aggressive: bool) -> dict[str, Any]:
  if kind == "slot15":
    return _SLOT15_AGGRESSIVE_ENTRY if aggressive else _SLOT15_PASSIVE_ENTRY
  return _HOURLY_AGGRESSIVE_ENTRY if aggressive else _HOURLY_PASSIVE_ENTRY


def runtime_preset(bot_kind: str, aggressive: bool) -> dict[str, Any]:
  """Cooldown (and hourly min-hold) overrides for passive vs aggressive."""
  base = dict(_AGGRESSIVE_RUNTIME if aggressive else _PASSIVE_RUNTIME)
  if bot_kind == "hourly":
    base["min_hold_seconds"] = 30
  return base


def effective_bot_entry_strategy(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  aggressive: bool,
  tuning: dict[str, Any] | None = None,
) -> EntryStrategyConfig:
  """Entry strategy from config, then passive/aggressive preset; auto-tune only when passive."""
  from src.backtest.mechanics_profiles import entry_kind_for_bot

  entry_kind = entry_kind_for_bot(kind)
  base = entry_strategy_from_cfg(cfg, kind=kind if kind == "hourly_trial_mech" else entry_kind)
  # If the config explicitly sets fields under entry_strategy, preserve them.
  raw_entry = (((cfg or {}).get("hourly") or {}).get("bot") or {}).get("entry_strategy") if entry_kind == "hourly" else (
    (((cfg or {}).get("intra_slot") or {}).get("bot") or {}).get("entry_strategy")
  )
  raw_entry = raw_entry or {}
  preset_kind = "slot15" if kind == "slot15" else "hourly"
  estrat = replace(base, **_entry_preset_map(preset_kind, aggressive))
  for field in EntryStrategyConfig.__dataclass_fields__:
    if field in raw_entry:
      estrat = replace(estrat, **{field: getattr(base, field)})
  if aggressive:
    return estrat
  if tuning and tuning.get("active"):
    kw: dict[str, Any] = {}
    if tuning.get("min_ask_edge_cents") is not None:
      kw["min_ask_edge_cents"] = float(tuning["min_ask_edge_cents"])
    if tuning.get("kelly_fraction") is not None:
      kw["kelly_fraction"] = float(tuning["kelly_fraction"])
    if kw:
      estrat = replace(estrat, **kw)
  return estrat


def apply_bot_runtime_settings(settings: Any, *, bot_kind: str, aggressive: bool | None = None) -> Any:
  """Apply passive/aggressive cooldown presets; preserve stored aggressive_entries flag."""
  flag = bool(aggressive if aggressive is not None else getattr(settings, "aggressive_entries", False))
  merged = settings.to_dict()
  merged["aggressive_entries"] = flag
  merged.update(runtime_preset(bot_kind, flag))
  if bot_kind == "slot15":
    from src.trading.slot15_bot_store import Slot15BotSettings

    return Slot15BotSettings.from_dict(merged)
  from src.trading.hourly_bot_store import HourlyBotSettings

  return HourlyBotSettings.from_dict(merged)
