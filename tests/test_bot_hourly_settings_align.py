"""Hourly bot settings are not mirrored between BTC and ETH."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.trading.bot_hourly_settings_align import align_eth_hourly_settings_from_btc
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def test_align_is_noop(tmp_path: Path):
  btc_store = HourlyBotStore(tmp_path / "hourly_bot_btc.db")
  eth_store = HourlyBotStore(tmp_path / "hourly_bot_eth.db")
  btc_store.save_settings(
    HourlyBotSettings(enabled=True, mode="live", max_spend_per_hour_usd=10.0)
  )
  eth_store.save_settings(
    HourlyBotSettings(enabled=False, mode="paper", max_spend_per_hour_usd=100.0)
  )

  loop = MagicMock()
  stats = align_eth_hourly_settings_from_btc(loop)
  assert stats["skipped"] is True

  eth = eth_store.get_settings()
  assert eth.mode == "paper"
  assert eth.enabled is False
  assert eth.max_spend_per_hour_usd == 100.0
