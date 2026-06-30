"""Tests for deployable bankroll and config budget sync."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.bot_budget import config_max_spend_per_hour, sync_max_spend_from_config
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def test_config_max_spend_per_hour_reads_hourly_bot():
  cfg = {"hourly": {"bot": {"max_spend_per_hour_usd": 10}}}
  assert config_max_spend_per_hour(cfg) == 10.0


def test_sync_max_spend_clamps_stored_cap_down_to_config():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=25.0))
    sync_max_spend_from_config(store, cfg={"hourly": {"bot": {"max_spend_per_hour_usd": 10}}})
    assert store.get_settings().max_spend_per_hour_usd == 10.0


def test_sync_max_spend_does_not_raise_stored_cap():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=8.0))
    sync_max_spend_from_config(store, cfg={"hourly": {"bot": {"max_spend_per_hour_usd": 10}}})
    assert store.get_settings().max_spend_per_hour_usd == 8.0
