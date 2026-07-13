"""SPX/NDX hourly live mirror — preflight and manager arm."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.index_live_experiment import (
  arm_index_live_mirror,
  disarm_index_live_mirror,
  index_live_mirror_active,
  run_index_live_preflight,
  seed_index_live_mirror_from_cfg,
  set_index_live_runtime_armed,
)
from src.trading.pnl_first_railway_manager import PnlFirstManagerConfig, enforce_sleep_lock


@pytest.fixture
def index_cfg(tmp_path):
  return {
    "paths": {"logs": str(tmp_path / "logs")},
    "pnl_first_manager": {
      "enforce_sleep": True,
      "phase": "prep",
      "lock_eth_live": False,
      "lock_slot15": False,
      "allow_index_paper": True,
      "allow_index_live": True,
    },
    "spx": {
      "enabled": True,
      "paths": {"logs": str(tmp_path / "spx" / "logs")},
      "hourly": {
        "enabled": True,
        "bot": {
          "enabled": True,
          "mode": "paper",
          "continuous_enabled": True,
          "live_mechanics_profile": "pnl_first",
          "paper_experiment": {"enabled": True},
          "live_mirror": {"enabled": False, "continuous_enabled": True},
        },
      },
      "live_settlement_index": {"enabled": True, "require_for_live_entries": False},
    },
    "ndx": {"enabled": False},
  }


def test_index_live_runtime_arm(tmp_path, index_cfg):
  assert not index_live_mirror_active(index_cfg, "spx")
  set_index_live_runtime_armed(index_cfg, "spx", True)
  assert index_live_mirror_active(index_cfg, "spx")
  set_index_live_runtime_armed(index_cfg, "spx", False)
  assert not index_live_mirror_active(index_cfg, "spx")


def test_seed_index_live_mirror(tmp_path, index_cfg):
  store = HourlyBotStore(tmp_path / "spx_live.db")
  store.save_settings(HourlyBotSettings.from_dict({"enabled": False, "mode": "paper"}))
  set_index_live_runtime_armed(index_cfg, "spx", True)
  result = seed_index_live_mirror_from_cfg(store, index_cfg, "spx")
  assert result.get("synced")
  assert store.get_settings().enabled is True
  assert store.get_settings().mode == "live"


def test_allow_index_live_arms_hourly_live_mirror(tmp_path, index_cfg):
  btc = HourlyBotStore(tmp_path / "btc.db")
  paper = HourlyBotStore(tmp_path / "spx_paper.db")
  live = HourlyBotStore(tmp_path / "spx_live.db")
  btc.save_settings(HourlyBotSettings.from_dict({"enabled": False, "mode": "paper"}))
  paper.save_settings(HourlyBotSettings.from_dict({
    "enabled": True,
    "mode": "paper",
    "continuous": True,
    "max_spend_per_hour_usd": 15.0,
  }))
  live.save_settings(HourlyBotSettings.from_dict({
    "enabled": False,
    "mode": "paper",
    "max_spend_per_hour_usd": 15.0,
  }))
  set_index_live_runtime_armed(index_cfg, "spx", True)

  loop = MagicMock()
  loop.cfg = index_cfg

  def _store(asset, kind="hourly"):
    if asset == "btc":
      return btc
    if asset == "spx" and kind == "hourly_live":
      return live
    if asset == "spx":
      return paper
    raise KeyError(asset)

  loop.hourly_bot_store.side_effect = _store
  loop.slot15_bot_store.return_value = MagicMock(
    get_settings=lambda: __import__(
      "src.trading.slot15_bot_store", fromlist=["Slot15BotSettings"]
    ).Slot15BotSettings.from_dict({"enabled": False, "mode": "paper"}),
    save_settings=lambda *a, **k: None,
  )
  loop.slot15_trial_bot_store.return_value = loop.slot15_bot_store.return_value

  mgr = PnlFirstManagerConfig.from_cfg(index_cfg)
  actions = enforce_sleep_lock(loop, mgr)

  assert paper.get_settings().mode == "paper"
  assert live.get_settings().enabled is True
  assert live.get_settings().mode == "live"
  assert any(a.get("action") == "index_live_arm" for a in actions)


def test_disarm_index_live_mirror(tmp_path, index_cfg):
  store = HourlyBotStore(tmp_path / "spx_live.db")
  store.save_settings(HourlyBotSettings.from_dict({"enabled": True, "mode": "live"}))
  set_index_live_runtime_armed(index_cfg, "spx", True)
  result = disarm_index_live_mirror(store, index_cfg, "spx")
  assert result.get("disarmed")
  assert not store.get_settings().enabled
  assert not index_live_mirror_active(index_cfg, "spx")


def test_preflight_blocks_when_allow_index_live_off(tmp_path, index_cfg):
  index_cfg["pnl_first_manager"]["allow_index_live"] = False
  loop = MagicMock()
  loop.cfg = index_cfg
  loop.index_hourly_prediction.return_value = {"ok": True, "event": {"event_ticker": "KXINXU-TEST"}}
  loop._kalshi_for.return_value = MagicMock(authenticated=True)

  paper_store = HourlyBotStore(tmp_path / "paper.db")
  paper_store.save_settings(HourlyBotSettings.from_dict({
    "enabled": True,
    "mode": "paper",
    "continuous": True,
  }))
  live_store = HourlyBotStore(tmp_path / "live.db")
  live_store.save_settings(HourlyBotSettings.from_dict({"enabled": False, "mode": "live"}))

  def _store(asset, kind="hourly"):
    if kind == "hourly_live":
      return live_store
    return paper_store

  loop.hourly_bot_store.side_effect = _store
  loop.hourly_live_reconcile.return_value = {"kalshi_only": [], "bot_only": []}

  pre = run_index_live_preflight(loop, index_cfg, "spx")
  assert not pre["ok"]
  assert "allow_index_live_off" in pre["issues"]
