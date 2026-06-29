"""Align ETH hourly bot persisted settings with BTC hourly (same toggles and risk caps)."""

from __future__ import annotations

import logging
from typing import Any

from src.trading.hourly_bot_store import HourlyBotSettings

log = logging.getLogger(__name__)


def align_eth_hourly_settings_from_btc(loop: Any) -> dict[str, Any]:
  """Copy BTC hourly bot_settings to ETH when they differ."""
  from src.assets import asset_enabled

  if not asset_enabled(loop.cfg, "eth"):
    return {"skipped": True, "reason": "eth_disabled"}

  btc_store = loop.hourly_bot_store("btc")
  eth_store = loop.hourly_bot_store("eth")
  btc = btc_store.get_settings()
  eth = eth_store.get_settings()
  btc_dict = btc.to_dict()
  eth_dict = eth.to_dict()

  if btc_dict == eth_dict:
    return {"aligned": False, "changed": []}

  changed = [k for k in btc_dict if btc_dict.get(k) != eth_dict.get(k)]
  eth_store.save_settings(
    HourlyBotSettings.from_dict(btc_dict),
    source="btc_hourly_align",
    cfg=loop._eth_cfg or loop.cfg,
  )
  log.info(
    "ETH hourly bot settings aligned from BTC hourly (%d fields): %s",
    len(changed),
    ", ".join(changed),
  )
  return {"aligned": True, "changed": changed, "settings": btc_dict}
