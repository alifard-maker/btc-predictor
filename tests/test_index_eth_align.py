"""SPX/NDX hourly paper trials mirror ETH hourly live (pnl_first)."""

from __future__ import annotations

import pytest

from src.assets import INDEX_ASSETS, asset_cfg
from src.backtest.mechanics_profiles import (
  apply_live_production_mechanics,
  live_mechanics_profile_for_cfg,
)
from src.config import load_config


@pytest.fixture
def base_cfg():
  return load_config()


def _eth_hourly_bot(base_cfg):
  return asset_cfg(base_cfg, "eth")["hourly"]["bot"]


@pytest.mark.parametrize("asset", INDEX_ASSETS)
def test_index_paper_uses_pnl_first_profile(base_cfg, asset):
  acfg = asset_cfg(base_cfg, asset)
  assert live_mechanics_profile_for_cfg(acfg) == "pnl_first"
  bot = acfg["hourly"]["bot"]
  assert bot.get("mode") == "paper"
  assert bot["late_entry"]["enabled"] is False
  assert bot["max_hours_to_settle_for_entry"] == 0.75


@pytest.mark.parametrize("asset", INDEX_ASSETS)
def test_index_paper_applies_pnl_first_mechanics_in_paper_mode(base_cfg, asset):
  acfg = asset_cfg(base_cfg, asset)
  out = apply_live_production_mechanics(acfg, kind="hourly", mode="paper")
  es = out["hourly"]["bot"]["entry_strategy"]
  assert es["min_ask_edge_cents"] == 18
  assert es["max_concurrent_positions"] == 2
  assert es["max_contracts_per_entry"] == 2


@pytest.mark.parametrize("asset", INDEX_ASSETS)
def test_index_mid_hour_entry_matches_eth(base_cfg, asset):
  eth_bot = _eth_hourly_bot(base_cfg)
  index_bot = asset_cfg(base_cfg, asset)["hourly"]["bot"]
  assert index_bot["min_hours_to_settle_for_entry"] == eth_bot["min_hours_to_settle_for_entry"]
  assert index_bot["max_hours_to_settle_for_entry"] == eth_bot["max_hours_to_settle_for_entry"]
  assert index_bot["late_entry"]["enabled"] == eth_bot["late_entry"]["enabled"]


@pytest.mark.parametrize("asset", INDEX_ASSETS)
def test_index_trial_scheduler_disabled(base_cfg, asset):
  trial = asset_cfg(base_cfg, asset)["hourly"]["bot"]["trial"]
  assert trial.get("continuous_enabled") is False
