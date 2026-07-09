"""Replay profiles for legacy vs current hourly live mechanics."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, Literal

from src.trading.live_entry_price import LiveEntryPricingConfig

MechanicsProfile = Literal["legacy", "mechanical_fixes", "current", "rally_only", "soft_rally", "pnl_first"]

HOURLY_TRIAL_KINDS = frozenset({
  "hourly_trial",
  "hourly_trial_rally",
  "hourly_trial_soft",
  "hourly_trial_mech",
})

HOURLY_V2_KIND = "hourly_v2"

_KIND_MECHANICS_PROFILE: dict[str, MechanicsProfile] = {
  "hourly_trial": "current",
  "hourly_trial_rally": "rally_only",
  "hourly_trial_soft": "soft_rally",
  "hourly_trial_mech": "mechanical_fixes",
}

PROFILE_LABELS: dict[str, str] = {
  "legacy": "Legacy (pre-084d7d1: cross-spread, no inventory/adaptive caps)",
  "mechanical_fixes": "Mechanical fixes only (084d7d1, no adaptive)",
  "current": "Current deploy (084d7d1 + adaptive passive v1)",
  "rally_only": "Rally-only (adaptive: entries only in rally mode; defense sits out)",
  "soft_rally": "Soft rally (rally mode full; defense: 1 YES threshold 40-80¢, edge≥15¢)",
  "pnl_first": "P&L-first Phase 0–1 (BTC S1 only, taker 15¢+, max 2 legs, no S2/tail)",
}


def is_hourly_trial_kind(kind: str) -> bool:
  return kind in HOURLY_TRIAL_KINDS


def mechanics_profile_for_kind(kind: str) -> MechanicsProfile | None:
  return _KIND_MECHANICS_PROFILE.get(kind)


def entry_kind_for_bot(kind: str) -> str:
  """Entry strategy / live guards use hourly mechanics for trial and v2 bot kinds."""
  if is_hourly_trial_kind(kind) or is_hourly_v2_kind(kind):
    return "hourly"
  return kind


def is_hourly_v2_kind(kind: str) -> bool:
  return kind == HOURLY_V2_KIND


def cfg_with_profile_for_kind(cfg: dict[str, Any], kind: str) -> dict[str, Any]:
  if is_hourly_v2_kind(kind):
    from src.assets import asset_v2_runtime_cfg

    return asset_v2_runtime_cfg(cfg)
  profile = mechanics_profile_for_kind(kind)
  if profile:
    return apply_mechanics_profile(cfg, profile)
  return cfg


def live_mechanics_profile_for_cfg(cfg: dict[str, Any] | None) -> MechanicsProfile | None:
  """Optional production-live profile under hourly.bot (BTC live only in config)."""
  raw = dict(((cfg or {}).get("hourly") or {}).get("bot") or {}).get("live_mechanics_profile")
  if not raw:
    return None
  profile = str(raw).strip().lower()
  if profile in PROFILE_LABELS:
    return profile  # type: ignore[return-value]
  return None


def apply_live_production_mechanics(
  cfg: dict[str, Any],
  *,
  kind: str,
  mode: str,
) -> dict[str, Any]:
  """Apply hourly.bot.live_mechanics_profile for production live bots (not trials)."""
  if kind != "hourly" or str(mode).lower() != "live":
    return cfg
  profile = live_mechanics_profile_for_cfg(cfg)
  if not profile:
    return cfg
  return apply_mechanics_profile(cfg, profile)


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
    live_exit["max_orphan_adopted_contracts"] = 6
    live_exit["adopted_leg_cut_loss_min_hold_seconds"] = 90
    live_exit["adopted_leg_cut_loss_min_usd"] = 0.20
    return c

  if profile == "mechanical_fixes":
    live_inventory["enabled"] = True
    live_adaptive["enabled"] = False
    live_entry["cross_spread_enabled"] = True
    live_exit["block_tail_entries"] = False
    live_exit.setdefault("max_resting_enters_per_hour", 24)
    live_exit.setdefault("max_orphan_adopted_contracts", 6)
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

  if profile == "pnl_first":
    live_inventory["enabled"] = True
    live_adaptive["enabled"] = False
    live_entry["cross_spread_enabled"] = True
    live_entry["cross_spread_min_edge_cents"] = 15.0
    live_entry["taker_only"] = True
    live_exit["block_tail_entries"] = True
    live_exit["tail_block_max_cents"] = 20
    live_exit.setdefault("max_resting_enters_per_hour", 6)
    es = bot.setdefault("entry_strategy", {})
    es["min_ask_edge_cents"] = 15
    es["tail_entry_block"] = True
    es["max_entries_per_cycle"] = 1
    es["max_concurrent_positions"] = 2
    es["allow_scale_in"] = False
    es["kelly_fraction"] = 0.12
    live_inventory["max_concurrent_positions"] = 2
    live_inventory["max_entries_per_cycle"] = 1
    live_inventory["max_same_side_threshold_legs"] = 1
    live_inventory["max_same_side_range_legs"] = 0
    live_inventory["allow_scale_in"] = False
    live_adaptive["defense_block_range_bands"] = True
    live_adaptive["rally_block_range_bands"] = True
    pf = dict(c.get("pnl_first") or {})
    late_h = float(pf.get("min_hours_to_settle_for_entry", 5 / 60))
    hourly = c.setdefault("hourly", {})
    hourly.setdefault("regime", {})["min_hours_to_settle"] = late_h
    bot["min_hours_to_settle_for_entry"] = late_h
    if "max_hours_to_settle_for_entry" in pf:
      bot["max_hours_to_settle_for_entry"] = float(pf["max_hours_to_settle_for_entry"])
    mh = dict(pf.get("mid_hour_entry") or {})
    if mh.get("enabled"):
      bot["min_hours_to_settle_for_entry"] = float(mh.get("min_hours_to_settle", 0.25))
      bot["max_hours_to_settle_for_entry"] = float(mh.get("max_hours_to_settle", 0.75))
      hourly.setdefault("regime", {})["min_hours_to_settle"] = float(
        mh.get("min_hours_to_settle", 0.25)
      )
    return c

  live_inventory["enabled"] = True
  live_adaptive["enabled"] = True
  live_entry["cross_spread_enabled"] = True
  return c


_ENTRY_PROFILE_OVERLAY_KEYS = (
  "defense_skip_all_entries",
  "defense_threshold_only",
  "defense_min_ask_edge_cents",
  "defense_yes_mid_min_cents",
  "defense_yes_mid_max_cents",
  "defense_block_range_bands",
  "rally_block_range_bands",
  "profit_lock_usd",
)


def apply_entry_profile_overlays(cfg: dict[str, Any], *, kind: str = "hourly") -> dict[str, Any]:
  """Merge enabled soft_rally / rally_only blocks onto live_adaptive for production bots."""
  if is_hourly_trial_kind(kind) and mechanics_profile_for_kind(kind):
    return cfg
  from src.trading.hourly_live_trial_align import skip_soft_rally_entry_overlay

  if skip_soft_rally_entry_overlay(cfg, kind=kind):
    return cfg
  c = copy.deepcopy(cfg)
  bot = (c.get("hourly") or {}).get("bot") or {}
  live_adaptive = dict(bot.get("live_adaptive") or {})
  soft = dict(bot.get("soft_rally") or {})
  rally = dict(bot.get("rally_only") or {})
  overlay: dict[str, Any] | None = None
  if soft.get("enabled"):
    overlay = soft
  elif rally.get("enabled"):
    overlay = rally
  if overlay:
    for key in _ENTRY_PROFILE_OVERLAY_KEYS:
      if key in overlay:
        live_adaptive[key] = overlay[key]
    c.setdefault("hourly", {}).setdefault("bot", {})["live_adaptive"] = live_adaptive
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
