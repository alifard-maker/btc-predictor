"""Tests for ETH hourly prediction support."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from src.assets import asset_cfg, asset_enabled, index_id_for_cfg
from src.config import load_config
from src.data.kalshi_daily import KalshiDailyMarkets
from src.db.hourly_store import SqliteHourlyStore


@pytest.fixture
def base_cfg():
  return load_config()


def test_asset_enabled_eth(base_cfg):
  assert asset_enabled(base_cfg, "btc") is True
  assert asset_enabled(base_cfg, "eth") is True


def test_asset_cfg_eth_overrides(base_cfg):
  eth = asset_cfg(base_cfg, "eth")
  assert eth["symbol"] == "ETH/USD"
  assert eth["_asset"] == "eth"
  assert eth["daily"]["threshold_series"] == ["KXETHD"]
  assert eth["daily"]["range_series"] == ["KXETH"]
  assert "eth" in eth["paths"]["candles"]
  assert index_id_for_cfg(eth) == "ETHUSD_RTI"


def test_asset_cfg_btc_unchanged(base_cfg):
  btc = asset_cfg(base_cfg, "btc")
  assert btc["symbol"] == "BTC/USD"
  assert index_id_for_cfg(btc) == "BRTI"


def test_asset_cfg_eth_inherits_hour_momentum_from_btc(base_cfg):
  eth = asset_cfg(base_cfg, "eth")
  hm = ((eth.get("hourly") or {}).get("bot") or {}).get("hour_momentum") or {}
  assert hm.get("enabled") is True
  assert eth["hourly"]["bot"].get("mirror_btc_settings") is True
  assert eth["hourly"]["bot"].get("experiment_start_at")


def test_asset_cfg_eth_inherits_quick_exit_and_hold_overlays(base_cfg):
  eth = asset_cfg(base_cfg, "eth")
  bot = (eth.get("hourly") or {}).get("bot") or {}
  qx = bot.get("quick_exit") or {}
  assert qx.get("enabled") is True
  assert qx.get("min_hold_seconds") == 30
  assert qx.get("cut_loss_min_usd") == 0.12
  holds = bot.get("hold_overlays") or {}
  assert holds.get("defense_min_hold_seconds") == 30
  assert holds.get("conservative_min_hold_seconds") == 30
  assert holds.get("rally_min_hold_seconds") == 90
  assert holds.get("pressing_min_hold_seconds") == 90


def test_asset_cfg_eth_inherits_soft_rally(base_cfg):
  eth = asset_cfg(base_cfg, "eth")
  soft = ((eth.get("hourly") or {}).get("bot") or {}).get("soft_rally") or {}
  assert soft.get("enabled") is True
  assert soft.get("defense_threshold_only") is True


def test_kalshi_daily_hourly_frequency_for_eth_series():
  cfg = {
    "kalshi": {"enabled": False},
    "daily": {
      "threshold_series": ["KXETHD"],
      "range_series": ["KXETH"],
    },
  }
  markets = KalshiDailyMarkets(cfg)
  from src.data.kalshi_daily import KalshiContractMarket
  from datetime import datetime, timezone

  now = datetime.now(timezone.utc)
  thresh = KalshiContractMarket(
    ticker="T1",
    event_ticker="KXETHD-TEST",
    title="ETH test",
    strike_type="greater",
    floor_strike=2500.0,
    cap_strike=None,
    close_time=now,
    open_time=now,
    yes_bid=0.4,
    yes_ask=0.42,
    subtitle="",
    series_ticker="KXETHD",
  )
  book = markets._make_book([thresh], [], "KXETHD", "KXETH")
  assert book is not None
  assert book.frequency == "hourly"


def test_hourly_store_asset_filter():
  with tempfile.TemporaryDirectory() as tmp:
    db = str(Path(tmp) / "hourly.db")
    btc_store = SqliteHourlyStore(db, asset="btc")
    eth_store = SqliteHourlyStore(db, asset="eth")
    btc_store.init()
    eth_store.init()

    row = {
      "logged_at": "2026-06-27T12:05:00+00:00",
      "event_ticker": "KXBTCD-TEST",
      "frequency": "hourly",
      "settle_time": "2026-06-27T13:00:00+00:00",
      "reference_price": 100000.0,
    }
    btc_store.log_prediction(row)

    eth_row = {**row, "event_ticker": "KXETHD-TEST", "reference_price": 2500.0}
    eth_store.log_prediction(eth_row)

    assert len(btc_store.load_recent(10)) == 1
    assert len(eth_store.load_recent(10)) == 1
    assert btc_store.get_by_event_ticker("KXETHD-TEST") is None
    assert eth_store.get_by_event_ticker("KXBTCD-TEST") is None

    assert btc_store.clear_all() == 1
    assert len(eth_store.load_recent(10)) == 1
