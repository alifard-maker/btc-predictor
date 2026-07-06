"""ETH hourly bot can mirror BTC hourly dashboard settings on startup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.trading.bot_hourly_settings_align import align_eth_hourly_settings_from_btc
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def test_align_skipped_when_mirror_disabled(tmp_path: Path):
  btc_store = HourlyBotStore(tmp_path / "hourly_bot_btc.db")
  eth_store = HourlyBotStore(tmp_path / "hourly_bot_eth.db")
  btc_store.save_settings(
    HourlyBotSettings(enabled=True, mode="live", max_spend_per_hour_usd=15.0)
  )
  eth_store.save_settings(
    HourlyBotSettings(enabled=False, mode="paper", max_spend_per_hour_usd=1.0)
  )

  loop = MagicMock()
  loop.cfg = {"eth": {"hourly": {"bot": {"mirror_btc_settings": False}}}}
  loop._eth_cfg = loop.cfg["eth"]
  loop.hourly_bot_store = lambda asset: btc_store if asset == "btc" else eth_store

  stats = align_eth_hourly_settings_from_btc(loop)
  assert stats["skipped"] is True

  eth = eth_store.get_settings()
  assert eth.mode == "paper"
  assert eth.enabled is False
  assert eth.max_spend_per_hour_usd == 1.0


def test_align_copies_btc_settings_when_enabled(tmp_path: Path):
  btc_store = HourlyBotStore(tmp_path / "hourly_bot_btc.db")
  eth_store = HourlyBotStore(tmp_path / "hourly_bot_eth.db")
  btc_store.save_settings(
    HourlyBotSettings(enabled=True, mode="live", max_spend_per_hour_usd=15.0)
  )
  eth_store.save_settings(
    HourlyBotSettings(enabled=False, mode="paper", max_spend_per_hour_usd=1.0)
  )

  loop = MagicMock()
  loop.cfg = {
    "eth": {"hourly": {"bot": {"mirror_btc_settings": True}}},
    "pnl_first_manager": {"lock_eth_live": False},
  }
  loop._eth_cfg = loop.cfg["eth"]
  loop.hourly_bot_store = lambda asset: btc_store if asset == "btc" else eth_store

  stats = align_eth_hourly_settings_from_btc(loop)
  assert stats["mirrored"] is True
  assert stats["mode"] == "live"
  assert stats["enabled"] is True

  eth = eth_store.get_settings()
  assert eth.mode == "live"
  assert eth.enabled is True
  assert eth.max_spend_per_hour_usd == 15.0


def test_align_skipped_when_lock_eth_live(tmp_path: Path):
  btc_store = HourlyBotStore(tmp_path / "hourly_bot_btc.db")
  eth_store = HourlyBotStore(tmp_path / "hourly_bot_eth.db")
  btc_store.save_settings(
    HourlyBotSettings(enabled=True, mode="live", max_spend_per_hour_usd=15.0)
  )
  eth_store.save_settings(
    HourlyBotSettings(enabled=False, mode="paper", max_spend_per_hour_usd=1.0)
  )

  loop = MagicMock()
  loop.cfg = {
    "eth": {"hourly": {"bot": {"mirror_btc_settings": True}}},
    "pnl_first_manager": {"lock_eth_live": True},
  }
  loop._eth_cfg = loop.cfg["eth"]
  loop.hourly_bot_store = lambda asset: btc_store if asset == "btc" else eth_store

  stats = align_eth_hourly_settings_from_btc(loop)
  assert stats["skipped"] is True
  assert stats["reason"] == "lock_eth_live"

  eth = eth_store.get_settings()
  assert eth.mode == "paper"
  assert eth.enabled is False
