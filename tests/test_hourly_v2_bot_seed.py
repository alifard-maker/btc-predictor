"""Hourly V2 bot settings seed must not overwrite dashboard toggles on restart."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.scheduler.hourly_v2_support import _seed_v2_bot_settings
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


@pytest.fixture
def v2_loop(tmp_path: Path):
  loop = MagicMock()
  loop.cfg = {
    "btc": {
      "hourly_v2": {
        "enabled": True,
        "bot": {
          "enabled": True,
          "mode": "paper",
          "max_spend_per_hour_usd": 15,
          "continuous_enabled": True,
        },
      },
    },
  }
  loop._btc_v2_cfg = loop.cfg["btc"]
  loop._eth_v2_cfg = None

  stores: dict[str, HourlyBotStore] = {}

  def hourly_bot_store(asset: str, kind: str = "hourly"):
    assert kind == "hourly_v2"
    key = asset
    if key not in stores:
      stores[key] = HourlyBotStore(tmp_path / f"hourly_v2_bot_{asset}.db")
    return stores[key]

  loop.hourly_bot_store = hourly_bot_store
  return loop, stores


def test_seed_applies_config_on_fresh_store(v2_loop):
  loop, stores = v2_loop
  _seed_v2_bot_settings(loop, "btc")
  settings = stores["btc"].get_settings()
  assert settings.enabled is True
  assert settings.max_spend_per_hour_usd == 15.0


def test_seed_does_not_reenable_after_dashboard_off(v2_loop):
  loop, stores = v2_loop
  store = loop.hourly_bot_store("btc", kind="hourly_v2")
  store.save_settings(HourlyBotSettings(enabled=False, max_spend_per_hour_usd=15.0))
  _seed_v2_bot_settings(loop, "btc")
  assert store.get_settings().enabled is False


def test_seed_skips_store_with_trade_history(v2_loop):
  loop, _stores = v2_loop
  store = loop.hourly_bot_store("btc", kind="hourly_v2")
  store.save_settings(HourlyBotSettings(enabled=False))
  store.log_trade(
    {
      "event_ticker": "KXBTCD-26JUL0417",
      "mode": "paper",
      "status": "filled",
      "action": "enter",
      "contracts": 1,
      "price_cents": 45,
      "cost_usd": 0.45,
    }
  )
  _seed_v2_bot_settings(loop, "btc")
  assert store.get_settings().enabled is False
