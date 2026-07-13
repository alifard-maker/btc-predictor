"""Tests for SPX/NDX index hourly support."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.assets import INDEX_ASSETS, SUPPORTED_ASSETS, asset_cfg, asset_enabled, index_id_for_cfg, is_index_asset
from src.config import load_config
from src.data.kalshi_daily import KalshiDailyMarkets, KalshiContractMarket
from src.trading.hourly_event_time import (
  hourly_asset_for_event,
  hourly_event_settle_utc,
  ticker_belongs_to_hourly_event,
)
from src.trading.us_market_hours import is_us_rth


@pytest.fixture
def base_cfg():
  return load_config()


def test_supported_assets_include_indices():
  assert "spx" in SUPPORTED_ASSETS
  assert "ndx" in SUPPORTED_ASSETS
  assert is_index_asset("spx")
  assert is_index_asset("ndx")
  assert not is_index_asset("btc")


def test_asset_cfg_spx_overrides(base_cfg):
  spx = asset_cfg(base_cfg, "spx")
  assert spx["_asset"] == "spx"
  assert spx["daily"]["threshold_series"] == ["KXINXU"]
  assert spx["daily"]["range_series"] == ["KXINX"]
  assert spx["price_feed"]["yfinance_ticker"] == "^GSPC"
  assert spx["hourly"]["bot"]["mode"] == "paper"
  assert spx["hourly"]["bot"]["live_mechanics_profile"] == "pnl_first"
  assert spx["hourly"]["bot"]["max_spend_per_hour_usd"] == 15
  assert index_id_for_cfg(spx) == "SPX"
  assert "spx" in spx["paths"]["candles"]


def test_asset_cfg_ndx_overrides(base_cfg):
  ndx = asset_cfg(base_cfg, "ndx")
  assert ndx["_asset"] == "ndx"
  assert ndx["daily"]["threshold_series"] == ["KXNASDAQ100U"]
  assert ndx["daily"]["range_series"] == ["KXNASDAQ100"]
  assert ndx["price_feed"]["yfinance_ticker"] == "^NDX"
  assert index_id_for_cfg(ndx) == "NDX"


def test_asset_enabled_indices(base_cfg):
  for asset in INDEX_ASSETS:
    assert asset_enabled(base_cfg, asset) is True


def test_index_event_time_suffix_parsing():
  settle = hourly_event_settle_utc("KXINXU-26JUL06H1000")
  assert settle is not None
  et = settle.astimezone(ZoneInfo("America/New_York"))
  assert et.year == 2026
  assert et.month == 7
  assert et.day == 6
  assert et.hour == 10
  assert et.minute == 0


def test_index_ticker_sibling_matching():
  ev = "KXINXU-26JUL06H1000"
  assert ticker_belongs_to_hourly_event("KXINXU-26JUL06H1000-T7619.9999", ev)
  assert ticker_belongs_to_hourly_event("KXINX-26JUL06H1000-B7537", ev)
  assert not ticker_belongs_to_hourly_event("KXINXU-26JUL06H1100-T7619.9999", ev)


def test_hourly_asset_for_index_events():
  assert hourly_asset_for_event("KXINXU-26JUL06H1000") == "spx"
  assert hourly_asset_for_event("KXINX-26JUL02H1300") == "spx"
  assert hourly_asset_for_event("KXINXDUD-26JUL02H1600") == "spx"
  assert hourly_asset_for_event("KXNASDAQ100U-26JUL06H1000") == "ndx"
  assert hourly_asset_for_event("KXNASDAQ100-26JUL02H1300") == "ndx"


def test_kalshi_daily_normalizes_greater_or_equal():
  cfg = {"kalshi": {"enabled": False}, "daily": {"threshold_series": ["KXINXU"], "range_series": ["KXINX"]}}
  markets = KalshiDailyMarkets(cfg)
  now = datetime.now(timezone.utc)
  row = {
    "ticker": "T1",
    "event_ticker": "KXINXU-TEST",
    "title": "test",
    "strike_type": "greater_or_equal",
    "floor_strike": 7600.0,
    "cap_strike": None,
    "close_time": now.isoformat(),
    "open_time": now.isoformat(),
    "yes_bid_dollars": "0.40",
    "yes_ask_dollars": "0.42",
  }
  parsed = markets._parse_market(row, "KXINXU")
  assert parsed is not None
  assert parsed.strike_type == "greater"


def test_us_rth_weekday_open():
  # Wed Jul 1 2026 10:00 ET
  now = datetime(2026, 7, 1, 14, 0, tzinfo=ZoneInfo("UTC"))
  assert is_us_rth(now=now)


def test_us_rth_weekend_closed():
  # Sat Jul 4 2026 10:00 ET
  now = datetime(2026, 7, 4, 14, 0, tzinfo=ZoneInfo("UTC"))
  assert not is_us_rth(now=now)


def test_live_hourly_pulse_includes_index_assets(base_cfg, tmp_path, monkeypatch):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  from src.scheduler.loop import PredictionLoop

  loop = PredictionLoop(base_cfg)
  pulse = loop.bot_risk_status()["live_hourly"]
  for asset in ("btc", "eth", "spx", "ndx"):
    assert asset in pulse
    row = pulse[asset]
    assert "enabled" in row
    assert "continuous" in row
    assert "mode" in row
    assert "cycles_total" in row
  assert "market_hours_open" in pulse["spx"]
  assert "market_hours_open" in pulse["ndx"]


def test_bootstrap_index_candles_if_missing_queues(tmp_path, monkeypatch):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  from src.config import load_config
  from src.data import index_candle_bootstrap as boot

  cfg = load_config()
  calls: list[str] = []
  monkeypatch.setattr(boot, "index_candles_missing", lambda _cfg, asset: asset == "spx")
  monkeypatch.setattr(
    boot,
    "bootstrap_index_candles",
    lambda asset, **kw: calls.append(asset) or tmp_path / f"{asset}.parquet",
  )
  boot._BOOTSTRAP_STARTED.clear()
  queued = boot.bootstrap_index_candles_if_missing(cfg, background=False)
  assert queued == ["spx"]
  assert calls == ["spx"]


def test_schedule_index_hourly_continuous_jobs(base_cfg):
  from apscheduler.schedulers.background import BackgroundScheduler

  from src.scheduler.index_hourly_support import schedule_index_hourly_jobs
  from src.scheduler.loop import PredictionLoop

  loop = PredictionLoop(base_cfg)
  scheduler = BackgroundScheduler()
  schedule_index_hourly_jobs(loop, scheduler)
  job_ids = {job.id for job in scheduler.get_jobs()}
  assert "spx_hourly_bot_continuous" in job_ids
  assert "ndx_hourly_bot_continuous" in job_ids
  assert "spx_hourly_trial_bot_continuous" not in job_ids
  assert "ndx_hourly_trial_bot_continuous" not in job_ids
  assert "spx_hourly_predict" in job_ids
  assert "ndx_hourly_predict" in job_ids
