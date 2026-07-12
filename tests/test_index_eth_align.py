"""SPX/NDX hourly live should mirror ETH hourly live execution profile."""

from __future__ import annotations

import pytest

from src.assets import INDEX_ASSETS, asset_cfg
from src.backtest.mechanics_profiles import live_mechanics_profile_for_cfg
from src.config import load_config


@pytest.fixture
def base_cfg():
  return load_config()


def _eth_hourly_bot(base_cfg):
  return asset_cfg(base_cfg, "eth")["hourly"]["bot"]


def test_eth_hourly_clears_btc_mechanical_profile(base_cfg):
  """ETH must not inherit BTC live_mechanics_profile via asset_cfg merge."""
  eth_cfg = asset_cfg(base_cfg, "eth")
  assert live_mechanics_profile_for_cfg(eth_cfg) is None
  assert eth_cfg["hourly"]["bot"].get("live_mechanics_profile") in ("", None)


@pytest.mark.parametrize("asset", INDEX_ASSETS)
def test_index_live_clears_btc_mechanical_profile(base_cfg, asset):
  bot = asset_cfg(base_cfg, asset)["hourly"]["bot"]
  assert bot.get("live_mechanics_profile") in ("", None)


@pytest.mark.parametrize("asset", INDEX_ASSETS)
def test_index_live_matches_eth_execution_keys(base_cfg, asset):
  eth_bot = _eth_hourly_bot(base_cfg)
  index_bot = asset_cfg(base_cfg, asset)["hourly"]["bot"]

  for key in (
    "live_trial_align",
    "soft_rally",
    "live_inventory",
    "live_exit",
    "live_adaptive",
    "whipsaw_guard",
    "entry_strategy",
  ):
    assert index_bot.get(key) == eth_bot.get(key), f"{asset} {key} differs from ETH"

  assert index_bot["live_trial_align"]["enabled"] is True
  assert index_bot["live_trial_align"]["live_exit_mode"] == "trial_legs"
  assert index_bot["soft_rally"]["enabled"] is True


@pytest.mark.parametrize("asset", INDEX_ASSETS)
def test_index_has_paper_trial_continuous(base_cfg, asset):
  trial = asset_cfg(base_cfg, asset)["hourly"]["bot"]["trial"]
  eth_trial = _eth_hourly_bot(base_cfg)["trial"]
  assert trial.get("continuous_enabled") is True
  assert trial.get("leg_take_profit_cents") == eth_trial.get("leg_take_profit_cents")
  assert trial.get("leg_stop_loss_cents") == eth_trial.get("leg_stop_loss_cents")
