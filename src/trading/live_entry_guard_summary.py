"""Summarize live hourly entry guards for dashboard / API diagnostics."""

from __future__ import annotations

from typing import Any


def build_live_entry_guard_summary(
  cfg: dict[str, Any] | None,
  *,
  mode: str,
  kind: str = "hourly",
  asset: str = "btc",
) -> dict[str, Any]:
  """Describe active live entry guards (helps explain fill-rate vs another asset)."""
  from src.backtest.mechanics_profiles import (
    PROFILE_LABELS,
    apply_live_production_mechanics,
    live_mechanics_profile_for_cfg,
  )
  from src.trading.bot_live_exit import live_exit_config
  from src.trading.hourly_live_trial_align import (
    HourlyLiveTrialAlignConfig,
    live_trial_align_active,
    skip_live_inventory_guards,
    skip_soft_rally_entry_overlay,
  )

  mode_l = str(mode).lower()
  if mode_l != "live" or kind != "hourly":
    return {"mode": mode_l, "kind": kind, "asset": asset}

  runtime_cfg = apply_live_production_mechanics(cfg or {}, kind=kind, mode=mode_l)
  profile = live_mechanics_profile_for_cfg(runtime_cfg)
  align = HourlyLiveTrialAlignConfig.from_cfg(runtime_cfg, kind=kind)
  live_exit = live_exit_config(runtime_cfg, kind=kind)
  bot = (runtime_cfg.get("hourly") or {}).get("bot") or {}
  inv = dict(bot.get("live_inventory") or {})
  adaptive_on = bool((bot.get("live_adaptive") or {}).get("enabled"))

  if profile:
    style = profile
    label = PROFILE_LABELS.get(profile, profile)
  elif live_trial_align_active(runtime_cfg, kind=kind, mode=mode_l):
    style = "standard_trial"
    label = "Standard trial align (paper trial parity)"
  else:
    style = "default"
    label = "Default live hourly"

  notes: list[str] = []
  if profile == "mechanical_fixes":
    notes.append("Adaptive/soft-rally entry filters OFF — typically more fills than standard trial.")
  elif profile == "pnl_first":
    notes.append("P&L-first Phase 0–1: S1 threshold only, no S2/tail, taker ≥15¢ edge, max 2 legs.")
  elif not skip_soft_rally_entry_overlay(runtime_cfg, kind=kind):
    notes.append("Soft-rally defense filters ON in defense mode (stricter threshold entries).")
  if live_exit.block_tail_entries:
    notes.append(f"Tail entries ≤{live_exit.tail_block_max_cents}¢ blocked in live.")
  if not skip_live_inventory_guards(runtime_cfg, kind=kind, mode=mode_l):
    notes.append("Live inventory caps enforced.")
    cap = inv.get("max_contracts_per_range_band_per_hour")
    if cap is not None:
      notes.append(f"S2 range cap: {cap} contracts per band per hour.")
  if align.block_reentry_while_resting:
    notes.append("Re-entry blocked while resting limit orders are open.")
  resting_cap = live_exit.max_resting_enters_per_hour
  if resting_cap and int(resting_cap) < 24:
    notes.append(f"Max {int(resting_cap)} resting enters per hour.")

  btc_hint: str | None = None
  if asset == "eth" and style == "standard_trial":
    btc_hint = (
      "BTC Hourly live uses mechanical_fixes (adaptive off, no S2 range cap, tail entries allowed). "
      "ETH uses standard trial align — expect fewer but stricter entries."
    )
  elif asset == "btc" and style == "pnl_first":
    btc_hint = (
      "BTC live is P&L-first Phase 0–1 (S1 only, selective taker entries). "
      "ETH stays paper until BTC hits 20 positive live hours."
    )
  elif asset == "btc" and style == "mechanical_fixes":
    btc_hint = (
      "BTC live mirrors Hourly Trial — Mech. ETH Hourly uses standard trial (stricter guards)."
    )

  return {
    "asset": asset,
    "mode": mode_l,
    "kind": kind,
    "live_execution_style": style,
    "live_execution_label": label,
    "mechanics_profile": profile,
    "adaptive_entries": adaptive_on,
    "block_tail_entries": live_exit.block_tail_entries,
    "inventory_guards": not skip_live_inventory_guards(runtime_cfg, kind=kind, mode=mode_l),
    "soft_rally_overlay": not skip_soft_rally_entry_overlay(runtime_cfg, kind=kind),
    "range_band_cap_per_hour": inv.get("max_contracts_per_range_band_per_hour"),
    "max_resting_enters_per_hour": resting_cap,
    "block_reentry_while_resting": align.block_reentry_while_resting,
    "notes": notes,
    "btc_comparison_hint": btc_hint,
  }
