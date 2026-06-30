"""Align ETH hourly bot persisted settings with BTC hourly (risk caps and filters only).

Mode (paper/live), Auto-bet (enabled), and auto-stop state are per-bot and never copied.
"""

from __future__ import annotations

import logging
from typing import Any

from src.trading.hourly_bot_store import HourlyBotSettings

log = logging.getLogger(__name__)

# Fields mirrored BTC → ETH on startup. Never include mode/enabled/auto-stop.
_ALIGN_FIELDS = (
  "max_spend_per_hour_usd",
  "allow_strong",
  "allow_actionable",
  "use_accumulated_profit",
  "profit_use_pct",
  "paper_auto_refill",
  "aggressive_entries",
  "take_profit_enabled",
  "take_profit_mode",
  "take_profit_pct",
  "take_profit_usd",
  "trail_arm_profit_pct",
  "trail_giveback_pct",
  "trail_arm_profit_usd",
  "trail_giveback_usd",
  "min_take_profit_pct",
  "max_take_profit_pct",
  "min_hold_seconds",
  "profit_exit_cooldown_seconds",
  "reentry_cooldown_seconds",
  "auto_stop_on_budget_exhausted",
)


def align_eth_hourly_settings_from_btc(loop: Any) -> dict[str, Any]:
  """Copy selected BTC hourly bot_settings fields to ETH when they differ."""
  from src.assets import asset_enabled

  if not asset_enabled(loop.cfg, "eth"):
    return {"skipped": True, "reason": "eth_disabled"}

  btc_store = loop.hourly_bot_store("btc")
  eth_store = loop.hourly_bot_store("eth")
  btc = btc_store.get_settings()
  eth = eth_store.get_settings()
  btc_dict = btc.to_dict()
  eth_dict = eth.to_dict()

  changed = [k for k in _ALIGN_FIELDS if btc_dict.get(k) != eth_dict.get(k)]
  if not changed:
    return {"aligned": False, "changed": []}

  merged = {**eth_dict, **{k: btc_dict[k] for k in changed}}
  eth_store.save_settings(
    HourlyBotSettings.from_dict(merged),
    source="btc_hourly_align",
    cfg=loop._eth_cfg or loop.cfg,
  )
  log.info(
    "ETH hourly bot settings aligned from BTC hourly (%d fields): %s",
    len(changed),
    ", ".join(changed),
  )
  return {"aligned": True, "changed": changed, "settings": {k: merged[k] for k in changed}}
