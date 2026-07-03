"""Scheduler support for SPX/NDX index hourly bots."""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from src.assets import INDEX_ASSETS, asset_cfg, asset_enabled, is_index_asset
from src.calibration.hourly_tracker import HourlyCalibrationTracker
from src.config import ensure_dirs
from src.data.storage import CandleStorage
from src.trading.us_market_hours import index_trading_allowed

log = logging.getLogger(__name__)


def init_index_assets(loop: Any) -> None:
  """Initialize SPX/NDX cfg, dirs, and calibration trackers on the loop."""
  loop._index_cfgs: dict[str, dict[str, Any]] = {}
  loop._index_storages: dict[str, CandleStorage] = {}
  loop._index_hourly_predictors: dict[str, Any] = {}
  loop._index_calibrations: dict[str, HourlyCalibrationTracker] = {}
  loop._index_ticker_caches: dict[str, tuple] = {}
  loop._latest_hourly_predictions: dict[str, dict[str, Any] | None] = {
    "btc": loop.latest_hourly_prediction,
    "eth": loop.latest_eth_hourly_prediction,
  }

  for asset in INDEX_ASSETS:
    if not asset_enabled(loop.cfg, asset):
      continue
    acfg = asset_cfg(loop.cfg, asset)
    loop._index_cfgs[asset] = acfg
    ensure_dirs(acfg)
    loop._index_calibrations[asset] = HourlyCalibrationTracker(acfg, asset=asset)
    loop._latest_hourly_predictions[asset] = None
    log.info("Initialized %s index hourly asset", asset.upper())


def acfg_for(loop: Any, asset: str) -> dict[str, Any]:
  asset = asset.lower()
  if asset == "btc":
    return loop.cfg
  if asset == "eth":
    return loop._eth_cfg or asset_cfg(loop.cfg, "eth")
  if asset in getattr(loop, "_index_cfgs", {}):
    return loop._index_cfgs[asset]
  if is_index_asset(asset) and asset_enabled(loop.cfg, asset):
    acfg = asset_cfg(loop.cfg, asset)
    loop._index_cfgs[asset] = acfg
    return acfg
  raise ValueError(f"Asset {asset} not configured")


def index_storage(loop: Any, asset: str) -> CandleStorage:
  asset = asset.lower()
  if asset not in loop._index_storages:
    loop._index_storages[asset] = CandleStorage(acfg_for(loop, asset))
  return loop._index_storages[asset]


def index_hourly_predictor(loop: Any, asset: str):
  asset = asset.lower()
  if asset not in loop._index_hourly_predictors:
    from src.models.hourly_predictor import HourlyPredictor

    loop._index_hourly_predictors[asset] = HourlyPredictor(acfg_for(loop, asset), asset=asset)
  return loop._index_hourly_predictors[asset]


def index_hourly_calibration(loop: Any, asset: str) -> HourlyCalibrationTracker:
  cal = loop._index_calibrations.get(asset.lower())
  if cal is None:
    raise RuntimeError(f"{asset.upper()} hourly is disabled")
  return cal


def cached_hourly_tab(loop: Any, asset: str) -> dict[str, Any] | None:
  asset = asset.lower()
  preds = getattr(loop, "_latest_hourly_predictions", None)
  if preds is not None:
    cached = preds.get(asset)
    if cached and cached.get("ok"):
      return cached
  if asset == "btc" and loop.latest_hourly_prediction and loop.latest_hourly_prediction.get("ok"):
    return loop.latest_hourly_prediction
  if asset == "eth" and loop.latest_eth_hourly_prediction and loop.latest_eth_hourly_prediction.get("ok"):
    return loop.latest_eth_hourly_prediction
  return None


def set_cached_hourly_tab(loop: Any, asset: str, tab: dict[str, Any]) -> None:
  asset = asset.lower()
  preds = getattr(loop, "_latest_hourly_predictions", None)
  if preds is not None:
    preds[asset] = tab
  if asset == "btc":
    loop.latest_hourly_prediction = tab
  elif asset == "eth":
    loop.latest_eth_hourly_prediction = tab


def hourly_prediction_fn(loop: Any, asset: str):
  asset = asset.lower()
  if asset == "btc":
    return loop.daily_prediction
  if asset == "eth":
    return loop.eth_hourly_prediction
  return lambda **kw: loop.index_hourly_prediction(asset, **kw)


def run_index_hourly_bot_continuous(loop: Any, asset: str) -> None:
  asset = asset.lower()
  if not asset_enabled(loop.cfg, asset):
    return
  acfg = acfg_for(loop, asset)
  if not index_trading_allowed(acfg):
    store = loop.hourly_bot_store(asset)
    store.set_last_skip_reason("outside_market_hours")
    return
  loop._run_hourly_bot_continuous(asset)


def run_index_hourly_prediction(loop: Any, asset: str, *, force: bool = False):
  asset = asset.lower()
  if not asset_enabled(loop.cfg, asset):
    return None
  acfg = acfg_for(loop, asset)
  if not acfg.get("hourly", {}).get("enabled", True):
    return None
  return loop._run_hourly_prediction_for_asset(asset, force=force)


def run_index_hourly_open_snapshot(loop: Any, asset: str):
  asset = asset.lower()
  if not asset_enabled(loop.cfg, asset):
    return None
  acfg = acfg_for(loop, asset)
  if not acfg.get("hourly", {}).get("enabled", True):
    return None
  return loop._run_hourly_open_for_asset(asset)


def run_index_hourly_late_call(loop: Any, asset: str, *, force: bool = False):
  asset = asset.lower()
  if not asset_enabled(loop.cfg, asset):
    return None
  acfg = acfg_for(loop, asset)
  if not acfg.get("hourly", {}).get("enabled", True):
    return None
  return loop._run_hourly_late_call_for_asset(asset, force=force)


def schedule_index_hourly_jobs(loop: Any, scheduler) -> None:
  for asset in INDEX_ASSETS:
    if not asset_enabled(loop.cfg, asset):
      continue
    acfg = acfg_for(loop, asset)
    if not acfg.get("hourly", {}).get("enabled", True):
      continue
    hcfg = acfg.get("hourly", {})
    prefix = asset
    if hcfg.get("hour_open_snapshot", True):
      open_minute = int(hcfg.get("open_log_minute", 0))
      scheduler.add_job(
        lambda a=asset: run_index_hourly_open_snapshot(loop, a),
        CronTrigger(minute=str(open_minute), timezone=loop.tz),
        id=f"{prefix}_hourly_open",
        max_instances=1,
      )
    minute = int(hcfg.get("log_minute", 5))
    scheduler.add_job(
      lambda a=asset: run_index_hourly_prediction(loop, a),
      CronTrigger(minute=str(minute), timezone=loop.tz),
      id=f"{prefix}_hourly_predict",
      max_instances=1,
    )
    late_minute = int(hcfg.get("late_call_minute", 45))
    scheduler.add_job(
      lambda a=asset: run_index_hourly_late_call(loop, a),
      CronTrigger(minute=str(late_minute), timezone=loop.tz),
      id=f"{prefix}_hourly_late_call",
      max_instances=1,
    )
    bot_cfg = hcfg.get("bot") or {}
    if bot_cfg.get("continuous_enabled", True):
      poll_sec = int(bot_cfg.get("poll_seconds", 12))
      scheduler.add_job(
        lambda a=asset: run_index_hourly_bot_continuous(loop, a),
        "interval",
        seconds=poll_sec,
        id=f"{prefix}_hourly_bot_continuous",
        max_instances=1,
      )
