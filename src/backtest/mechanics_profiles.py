"""Replay profiles for legacy vs current hourly live mechanics."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, Literal

from src.trading.live_entry_price import LiveEntryPricingConfig

MechanicsProfile = Literal["legacy", "mechanical_fixes", "current", "rally_only", "soft_rally"]

PROFILE_LABELS: dict[str, str] = {
  "legacy": "Legacy (pre-084d7d1: cross-spread, no inventory/adaptive caps)",
  "mechanical_fixes": "Mechanical fixes only (084d7d1, no adaptive)",
  "current": "Current deploy (084d7d1 + adaptive passive v1)",
  "rally_only": "Rally-only (adaptive: entries only in rally mode; defense sits out)",
  "soft_rally": "Soft rally (rally mode full; defense: 1 YES threshold 40-80¢, edge≥15¢)",
}


def apply_mechanics_profile(cfg: dict[str, Any], profile: MechanicsProfile) -> dict[str, Any]:
  """Return a deep-copied config with bot mechanics matching a deployment era."""
  c = copy.deepcopy(cfg)
  hourly = c.setdefault("hourly", {})
  bot = hourly.setdefault("bot", {})
  live_exit = bot.setdefault("live_exit", {})
  live_inventory = bot.setdefault("live_inventory", {})
  live_adaptive = bot.setdefault("live_adaptive", {})
  live_entry = bot.setdefault("live_entry", {})

  if profile == "legacy":
    live_inventory["enabled"] = False
    live_adaptive["enabled"] = False
    live_entry["cross_spread_enabled"] = True
    live_entry["cross_spread_min_edge_cents"] = 12.0
    live_exit["max_resting_enters_per_hour"] = 999
    live_exit["max_adopted_contracts"] = 6
    live_exit["adopted_leg_cut_loss_min_hold_seconds"] = 90
    live_exit["adopted_leg_cut_loss_min_usd"] = 0.20
    return c

  if profile == "mechanical_fixes":
    live_inventory["enabled"] = True
    live_adaptive["enabled"] = False
    live_entry["cross_spread_enabled"] = True
    live_exit.setdefault("max_resting_enters_per_hour", 6)
    live_exit.setdefault("max_adopted_contracts", 2)
    return c

  if profile == "rally_only":
    live_inventory["enabled"] = True
    live_adaptive["enabled"] = True
    live_adaptive["defense_skip_all_entries"] = True
    live_adaptive["rally_block_range_bands"] = True
    live_entry["cross_spread_enabled"] = True
    return c

  if profile == "soft_rally":
    live_inventory["enabled"] = True
    live_adaptive["enabled"] = True
    live_adaptive["defense_skip_all_entries"] = False
    live_adaptive["defense_threshold_only"] = True
    live_adaptive["defense_min_ask_edge_cents"] = 15.0
    live_adaptive["defense_yes_mid_min_cents"] = 40
    live_adaptive["defense_yes_mid_max_cents"] = 80
    live_adaptive["defense_block_range_bands"] = True
    live_adaptive["rally_block_range_bands"] = True
    live_entry["cross_spread_enabled"] = True
    return c

  live_inventory["enabled"] = True
  live_adaptive["enabled"] = True
  live_entry["cross_spread_enabled"] = True
  return c


def replay_entry_pricing(
  cfg: dict[str, Any],
  *,
  profile: MechanicsProfile,
  aggressive: bool,
  kind: str = "hourly",
) -> LiveEntryPricingConfig:
  """Pricing for replay: legacy/mechanical_fixes allow cross-spread in passive replay."""
  from src.trading.live_entry_price import live_entry_pricing_from_cfg

  if profile == "legacy":
    bot_cfg = ((cfg.get("hourly") or {}).get("bot") or {})
    pricing = LiveEntryPricingConfig.from_bot_cfg(bot_cfg)
    if aggressive:
      return replace(pricing, cross_spread_min_edge_cents=10.0)
    return pricing

  if profile == "mechanical_fixes":
    bot_cfg = ((cfg.get("hourly") or {}).get("bot") or {})
    pricing = LiveEntryPricingConfig.from_bot_cfg(bot_cfg)
    if aggressive:
      return replace(pricing, cross_spread_min_edge_cents=10.0)
    return replace(pricing, cross_spread_enabled=False)

  return live_entry_pricing_from_cfg(cfg, kind=kind, aggressive=aggressive)
