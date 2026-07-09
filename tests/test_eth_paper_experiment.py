"""Tests for ETH paper experiment sync and health."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.trading.eth_paper_experiment import (
  check_eth_paper_harness,
  seed_eth_paper_settings_from_cfg,
  settings_patch_from_eth_bot_yaml,
)
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


@pytest.fixture
def eth_cfg():
  return {
    "pnl_first": {"mid_hour_entry": {"eth_paper_enabled": True}},
    "eth": {
      "hourly": {
        "bot": {
          "enabled": True,
          "mode": "paper",
          "continuous_enabled": True,
          "max_spend_per_hour_usd": 15.0,
          "paper_auto_refill": True,
          "profit_use_pct": 100.0,
          "min_hold_seconds": 90,
          "experiment_start_at": "2026-07-09T12:00:00+00:00",
          "paper_experiment": {"enabled": True, "sync_settings_on_arm": True},
          "soft_rally": {"enabled": False},
          "whipsaw_guard": {"block_entries_when_regime_blocked": False},
        },
      },
    },
  }


def test_settings_patch_maps_yaml_fields():
  patch = settings_patch_from_eth_bot_yaml({
    "enabled": True,
    "mode": "paper",
    "continuous_enabled": True,
    "paper_auto_refill": True,
    "profit_use_pct": 100.0,
    "min_hold_seconds": 90,
  })
  assert patch["continuous"] is True
  assert patch["paper_auto_refill"] is True
  assert patch["profit_use_pct"] == 100.0
  assert patch["auto_stopped"] is False


def test_seed_eth_paper_settings_fixes_stale_sqlite(tmp_path: Path, eth_cfg):
  store = HourlyBotStore(tmp_path / "eth.db")
  store.save_settings(HourlyBotSettings.from_dict({
    "enabled": False,
    "mode": "paper",
    "continuous": False,
    "profit_use_pct": 30.0,
    "paper_auto_refill": False,
    "max_spend_per_hour_usd": 15.0,
  }))

  result = seed_eth_paper_settings_from_cfg(store, eth_cfg)
  assert result["ok"] is True
  assert result["synced"] is True
  assert "enabled" in result["changed_fields"]

  s = store.get_settings()
  assert s.enabled is True
  assert s.continuous is True
  assert s.paper_auto_refill is True
  assert s.profit_use_pct == 100.0


def test_check_eth_paper_harness_flags_disabled():
  from unittest.mock import MagicMock

  loop = MagicMock()
  store = MagicMock()
  store.get_settings.return_value = HourlyBotSettings.from_dict({
    "enabled": False,
    "mode": "paper",
    "continuous": True,
  })
  store.last_skip_reason.return_value = "auto_bet_off"
  loop.hourly_bot_store.return_value = store

  cfg = {
    "eth": {"hourly": {"bot": {"paper_experiment": {"enabled": True}}}},
  }
  out = check_eth_paper_harness(loop, cfg)
  assert out["ok"] is False
  assert "eth_paper_disabled" in out["issues"]
  assert "eth_fatal_skip:auto_bet_off" in out["issues"]
