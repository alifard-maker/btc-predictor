"""Hourly trial bot mechanics profile mapping and application."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.backtest.mechanics_profiles import (
  cfg_with_profile_for_kind,
  entry_kind_for_bot,
  is_hourly_trial_kind,
  mechanics_profile_for_kind,
)
from src.trading.hourly_bot import HourlyBot
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


@pytest.mark.parametrize(
  ("kind", "expected"),
  [
    ("hourly_trial", "current"),
    ("hourly_trial_rally", "rally_only"),
    ("hourly_trial_soft", "soft_rally"),
    ("hourly_trial_mech", "mechanical_fixes"),
    ("hourly", None),
    ("slot15", None),
  ],
)
def test_mechanics_profile_for_kind(kind, expected):
  assert mechanics_profile_for_kind(kind) == expected


@pytest.mark.parametrize(
  "kind",
  ["hourly_trial", "hourly_trial_rally", "hourly_trial_soft", "hourly_trial_mech"],
)
def test_is_hourly_trial_kind_true(kind):
  assert is_hourly_trial_kind(kind)


def test_is_hourly_trial_kind_false_for_live():
  assert not is_hourly_trial_kind("hourly")


def test_entry_kind_for_bot_maps_trials_to_hourly():
  assert entry_kind_for_bot("hourly_trial_rally") == "hourly"
  assert entry_kind_for_bot("hourly") == "hourly"


def test_cfg_with_profile_for_kind_rally_only_skips_defense():
  base = {"hourly": {"bot": {"live_adaptive": {"defense_skip_all_entries": False}}}}
  out = cfg_with_profile_for_kind(base, "hourly_trial_rally")
  adaptive = out["hourly"]["bot"]["live_adaptive"]
  assert adaptive["defense_skip_all_entries"] is True
  assert adaptive["enabled"] is True


def test_cfg_with_profile_for_kind_soft_rally_defense_threshold():
  base = {"hourly": {"bot": {"live_adaptive": {}}}}
  out = cfg_with_profile_for_kind(base, "hourly_trial_soft")
  adaptive = out["hourly"]["bot"]["live_adaptive"]
  assert adaptive["defense_threshold_only"] is True
  assert adaptive["defense_min_ask_edge_cents"] == 15.0
  assert adaptive["defense_yes_mid_min_cents"] == 40
  assert adaptive["defense_yes_mid_max_cents"] == 80


def test_cfg_with_profile_for_kind_mech_adaptive_off():
  base = {"hourly": {"bot": {"live_adaptive": {"enabled": True}, "live_inventory": {"enabled": False}}}}
  out = cfg_with_profile_for_kind(base, "hourly_trial_mech")
  bot = out["hourly"]["bot"]
  assert bot["live_adaptive"]["enabled"] is False
  assert bot["live_inventory"]["enabled"] is True


def test_hourly_bot_applies_mechanics_profile_in_continuous_cycle():
  with tempfile.TemporaryDirectory() as tmp, patch.object(
    HourlyBot, "_process_exits", return_value=[],
  ), patch.object(HourlyBot, "_process_entries", return_value=[]) as mock_entries:
    store = HourlyBotStore(Path(tmp) / "hourly_trial_rally_bot_btc.db")
    settings = store.get_settings()
    store.save_settings(HourlyBotSettings(**{**settings.to_dict(), "enabled": True, "continuous": True}))
    bot = HourlyBot(store, asset="btc", kind="hourly_trial_rally")
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXBTCHOUR-TEST"},
      "live": {"hours_to_settle": 0.5, "regime": {"allow_trade": True}},
    }
    cfg = {"hourly": {"bot": {"live_adaptive": {"defense_skip_all_entries": False}}}}
    bot.run_continuous_cycle(tab, cfg=cfg)
    assert mock_entries.called
    passed_cfg = mock_entries.call_args.args[3]
    assert passed_cfg["hourly"]["bot"]["live_adaptive"]["defense_skip_all_entries"] is True
