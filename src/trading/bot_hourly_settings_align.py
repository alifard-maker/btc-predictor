"""Mirror BTC hourly bot dashboard settings onto ETH hourly when configured."""

from __future__ import annotations

import logging
from typing import Any

from src.assets import asset_cfg
from src.trading.hourly_bot_store import HourlyBotSettings

log = logging.getLogger(__name__)


def align_eth_hourly_settings_from_btc(loop: Any) -> dict[str, Any]:
  """Copy BTC hourly bot store settings to ETH when eth.hourly.bot.mirror_btc_settings is true."""
  eth_cfg = loop._eth_cfg or asset_cfg(loop.cfg, "eth")
  bot_cfg = (eth_cfg.get("hourly") or {}).get("bot") or {}
  if not bot_cfg.get("mirror_btc_settings"):
    return {"skipped": True, "reason": "mirror_btc_settings_disabled"}

  btc_store = loop.hourly_bot_store("btc")
  eth_store = loop.hourly_bot_store("eth")
  btc = btc_store.get_settings()
  eth_before = eth_store.get_settings()

  if btc.to_dict() == eth_before.to_dict():
    return {
      "mirrored": False,
      "unchanged": True,
      "mode": btc.mode,
      "enabled": btc.enabled,
      "max_spend_per_hour_usd": btc.max_spend_per_hour_usd,
    }

  eth_store.save_settings(
    HourlyBotSettings.from_dict(btc.to_dict()),
    source="mirror_btc",
    cfg=eth_cfg,
  )
  log.info(
    "ETH hourly bot settings mirrored from BTC: mode=%s enabled=%s max_spend=%.2f",
    btc.mode,
    btc.enabled,
    btc.max_spend_per_hour_usd,
  )
  return {
    "mirrored": True,
    "mode": btc.mode,
    "enabled": btc.enabled,
    "max_spend_per_hour_usd": btc.max_spend_per_hour_usd,
    "eth_before_mode": eth_before.mode,
    "eth_before_enabled": eth_before.enabled,
  }
