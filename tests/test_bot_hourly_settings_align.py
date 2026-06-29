"""ETH hourly bot settings alignment with BTC hourly."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.trading.bot_hourly_settings_align import align_eth_hourly_settings_from_btc
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def test_align_eth_hourly_settings_from_btc(tmp_path: Path):
  btc_store = HourlyBotStore(tmp_path / "hourly_bot_btc.db")
  eth_store = HourlyBotStore(tmp_path / "hourly_bot_eth.db")
  btc_store.save_settings(
    HourlyBotSettings(enabled=True, use_accumulated_profit=False, max_spend_per_hour_usd=100.0)
  )
  eth_store.save_settings(
    HourlyBotSettings(enabled=True, use_accumulated_profit=True, max_spend_per_hour_usd=100.0)
  )

  loop = MagicMock()
  loop.cfg = {"eth": {"enabled": True}}
  loop._eth_cfg = loop.cfg
  loop.hourly_bot_store = lambda asset: btc_store if asset == "btc" else eth_store

  stats = align_eth_hourly_settings_from_btc(loop)
  assert stats["aligned"] is True
  assert "use_accumulated_profit" in stats["changed"]
  assert eth_store.get_settings().use_accumulated_profit is False


def test_align_eth_hourly_settings_idempotent(tmp_path: Path):
  btc_store = HourlyBotStore(tmp_path / "hourly_bot_btc.db")
  eth_store = HourlyBotStore(tmp_path / "hourly_bot_eth.db")
  settings = HourlyBotSettings(enabled=True, use_accumulated_profit=False)
  btc_store.save_settings(settings)
  eth_store.save_settings(settings)

  loop = MagicMock()
  loop.cfg = {"eth": {"enabled": True}}
  loop._eth_cfg = loop.cfg
  loop.hourly_bot_store = lambda asset: btc_store if asset == "btc" else eth_store

  stats = align_eth_hourly_settings_from_btc(loop)
  assert stats["aligned"] is False
