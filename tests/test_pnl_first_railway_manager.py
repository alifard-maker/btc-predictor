"""Tests for Railway P&L-first manager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.trading.hourly_bot_store import HourlyBotSettings
from src.trading.pnl_first_railway_manager import (
  PnlFirstManagerConfig,
  enforce_sleep_lock,
  run_preflight,
)


@pytest.fixture
def mgr_cfg():
  return {
    "pnl_first_manager": {
      "enabled": True,
      "phase": "prep",
      "enforce_sleep": True,
      "trading_armed": False,
    },
    "hourly": {"bot": {"live_mechanics_profile": "pnl_first", "experiment_start_at": "2026-07-04T16:59:00+00:00"}},
    "pnl_first": {"milestone_positive_hours": 20},
  }


def test_enforce_sleep_lock_disables_enabled_btc(tmp_path, mgr_cfg):
  from src.trading.hourly_bot_store import HourlyBotStore

  store = HourlyBotStore(tmp_path / "btc.db")
  store.save_settings(HourlyBotSettings.from_dict({"enabled": True, "mode": "live", "max_spend_per_hour_usd": 15.0}))

  loop = MagicMock()
  loop.hourly_bot_store.return_value = store
  mgr = PnlFirstManagerConfig.from_cfg(mgr_cfg)

  actions = enforce_sleep_lock(loop, mgr)
  assert actions and actions[0]["action"] == "sleep_lock"
  assert store.get_settings().enabled is False


def test_poa_live_skips_btc_sleep_lock_but_still_locks_eth(tmp_path, mgr_cfg, monkeypatch):
  from src.trading.hourly_bot_store import HourlyBotStore
  from src.trading.slot15_bot_store import Slot15BotSettings
  import src.trading.pnl_first_railway_manager as mgr_mod

  btc = HourlyBotStore(tmp_path / "btc.db")
  eth = HourlyBotStore(tmp_path / "eth.db")
  btc.save_settings(HourlyBotSettings.from_dict({"enabled": True, "mode": "live", "max_spend_per_hour_usd": 15.0}))
  eth.save_settings(HourlyBotSettings.from_dict({"enabled": True, "mode": "live", "max_spend_per_hour_usd": 15.0}))

  def _store(asset, kind="hourly"):
    return btc if asset == "btc" else eth

  slot15_off = MagicMock(
    get_settings=lambda: Slot15BotSettings.from_dict({"enabled": False, "mode": "paper"}),
    save_settings=lambda *a, **k: None,
  )

  loop = MagicMock()
  loop.cfg = mgr_cfg
  loop.hourly_bot_store.side_effect = _store
  loop.slot15_bot_store.return_value = slot15_off
  loop.slot15_trial_bot_store.return_value = slot15_off
  monkeypatch.setattr(mgr_mod, "load_manager_state", lambda _cfg: {"poa_live_active": True})

  mgr = PnlFirstManagerConfig.from_cfg({**mgr_cfg, "pnl_first_manager": {**mgr_cfg["pnl_first_manager"], "lock_eth_live": True}})
  actions = enforce_sleep_lock(loop, mgr)

  assert btc.get_settings().enabled is True
  assert eth.get_settings().enabled is False
  assert eth.get_settings().mode == "paper"
  assert any(a.get("asset") == "eth" for a in actions)
  assert not any(a.get("asset") == "btc" and a.get("kind") == "hourly" for a in actions)


def test_sleep_lock_disables_btc_and_eth_slot15(tmp_path, mgr_cfg):
  from src.trading.hourly_bot_store import HourlyBotStore
  from src.trading.slot15_bot_store import Slot15BotStore, Slot15BotSettings

  btc15 = Slot15BotStore(tmp_path / "btc15.db")
  eth15 = Slot15BotStore(tmp_path / "eth15.db")
  btc15.save_settings(Slot15BotSettings.from_dict({"enabled": True, "mode": "live", "max_spend_per_slot_usd": 25.0}))
  eth15.save_settings(Slot15BotSettings.from_dict({"enabled": True, "mode": "live", "max_spend_per_slot_usd": 25.0}))

  loop = MagicMock()
  loop.cfg = mgr_cfg
  loop.hourly_bot_store.return_value = MagicMock(
    get_settings=lambda: HourlyBotSettings.from_dict({"enabled": False, "mode": "paper"}),
    save_settings=lambda *a, **k: None,
  )
  loop.slot15_bot_store.side_effect = lambda asset: btc15 if asset == "btc" else eth15
  loop.slot15_trial_bot_store.side_effect = lambda asset: MagicMock(
    get_settings=lambda: Slot15BotSettings.from_dict({"enabled": False, "mode": "paper"}),
    save_settings=lambda *a, **k: None,
  )

  mgr = PnlFirstManagerConfig.from_cfg({**mgr_cfg, "pnl_first_manager": {**mgr_cfg["pnl_first_manager"], "lock_slot15": True}})
  actions = enforce_sleep_lock(loop, mgr)

  assert btc15.get_settings().enabled is False
  assert btc15.get_settings().mode == "paper"
  assert eth15.get_settings().enabled is False
  assert eth15.get_settings().mode == "paper"
  assert any(a.get("kind") == "slot15" and a.get("asset") == "btc" for a in actions)
  assert any(a.get("kind") == "slot15" and a.get("asset") == "eth" for a in actions)


def test_preflight_flags_missing_hourly_tab(mgr_cfg):
  loop = MagicMock()
  loop.cfg = mgr_cfg
  loop.daily_prediction.return_value = {"ok": False}
  loop.hourly_bot_store.return_value = MagicMock(
    get_settings=lambda: HourlyBotSettings.from_dict({"enabled": False, "mode": "live", "max_spend_per_hour_usd": 15.0}),
    all_open_live_positions=lambda: [],
    open_positions=lambda _: [],
  )
  loop.hourly_live_reconcile.return_value = {"kalshi_only": [], "bot_only": []}
  loop.kalshi = None

  out = run_preflight(loop, mgr_cfg)
  assert "hourly_tab_unavailable" in out["issues"]
