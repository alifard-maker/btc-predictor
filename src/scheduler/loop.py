from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.calibration.tracker import CalibrationTracker
from src.config import ensure_dirs, load_config
from src.data.fetcher import DataFetcher
from src.data.storage import CandleStorage
from src.features.slots import floor_to_15m, slot_end
from src.logging.prediction_log import PredictionLogger
from src.models.predictor import Prediction, Predictor

log = logging.getLogger(__name__)


class PredictionLoop:
  """Fetch data every minute; predict at :00, :15, :30, :45 for next 15m slot."""

  def __init__(self, cfg: dict[str, Any] | None = None, model_path: str | None = None):
    self.cfg = cfg or load_config()
    ensure_dirs(self.cfg)

    self.fetcher = DataFetcher(self.cfg)
    self.storage = CandleStorage(self.cfg)
    self.predictor = Predictor(self.cfg, model_path=model_path or self._default_model_path())
    self.logger = PredictionLogger(self.cfg)
    self.calibration = CalibrationTracker(self.cfg)
    self.tz = self.cfg.get("timezone", "America/New_York")
    self.horizon = self.cfg.get("prediction_horizon_minutes", 15)
    self.min_candles = self.cfg.get("min_candles_15m", 48)
    self.fetch_15m_count = self.cfg.get("fetch_candles_15m", 64)
    self.latest_prediction: Prediction | None = None
    self.last_error: str | None = None
    self._scheduler: BackgroundScheduler | BlockingScheduler | None = None

  def _default_model_path(self) -> str | None:
    p = Path(self.cfg["paths"]["models"]) / "model.joblib"
    return str(p) if p.exists() else None

  def fetch_and_store(self) -> None:
    try:
      df_1m = self.fetcher.fetch_latest_candles("1m", count=240)
      if not df_1m.empty:
        self.storage.save("1m", df_1m)
    except Exception as e:
      log.warning("Failed to fetch 1m: %s", e)
      self.last_error = str(e)

    try:
      df_15m = self.fetcher.fetch_latest_candles("15m", count=self.fetch_15m_count)
      if not df_15m.empty:
        self.storage.save("15m", df_15m)
    except Exception as e:
      log.warning("Failed to fetch 15m: %s", e)
      self.last_error = str(e)

  def resolve_outcomes(self) -> None:
    df_15m = self.storage.load("15m")
    if df_15m.empty:
      return

    df_15m = df_15m.copy()
    df_15m["timestamp"] = pd.to_datetime(df_15m["timestamp"], utc=True)

    pending = self.calibration.get_pending()
    price_lookup = {}
    for _row_id, ts_str, entry_price in pending:
      slot_s = floor_to_15m(pd.Timestamp(ts_str), self.tz)
      slot_e = slot_end(slot_s)
      # Exit price = close of the 15m candle ending at slot_e
      match = df_15m[df_15m["timestamp"] >= slot_e]
      if match.empty:
        continue
      exit_price = float(match.iloc[0]["close"])
      actual_return = (exit_price - entry_price) / entry_price
      price_lookup[ts_str] = (exit_price, actual_return)

    if price_lookup:
      resolved = self.calibration.resolve_with_prices(price_lookup)
      log.info("Resolved %d predictions", resolved)

  def run_prediction(self) -> Prediction | None:
    try:
      self.resolve_outcomes()
      self.fetch_and_store()

      df_15m = self.storage.load("15m")
      df_1m = self.storage.load("1m")

      if df_15m.empty or len(df_15m) < self.min_candles:
        self.fetch_and_store()
        df_15m = self.storage.load("15m")
        df_1m = self.storage.load("1m")

      if df_15m.empty or len(df_15m) < self.min_candles:
        self.last_error = f"Need {self.min_candles}+ fifteen-minute candles, have {len(df_15m)}"
        log.error(self.last_error)
        return None

      pred = self.predictor.predict(df_15m, df_1m if not df_1m.empty else None)
      self.logger.log(pred)
      self.latest_prediction = pred
      self.last_error = None
      log.info(
        "Slot %s: UP=%.1f%% signal=%s price=$%.2f",
        pred.slot_label, pred.prob_up * 100, pred.signal.value, pred.price,
      )
      return pred
    except Exception as e:
      self.last_error = str(e)
      log.exception("Prediction failed: %s", e)
      return None

  def status(self) -> dict[str, Any]:
    df_15m = self.storage.load("15m")
    df_1m = self.storage.load("1m")
    return {
      "symbol": self.cfg["symbol"],
      "exchange": getattr(self.fetcher, "_exchange_id", None) or self.cfg.get("exchange"),
      "exchange_connected": self.fetcher.is_connected(),
      "model": "trained" if self.predictor.model else "baseline",
      "primary_timeframe": "15m",
      "candles_15m": len(df_15m),
      "candles_1m": len(df_1m),
      "min_candles_15m": self.min_candles,
      "lookback_hours": self.cfg.get("lookback_hours", 12),
      "slot_context": "1h + 4h (primary) + 12h",
      "volume_spike_window": f"{self.cfg.get('features', {}).get('volume_spike_window', 16)}×15m",
      "latest_candle_15m": df_15m["timestamp"].iloc[-1].isoformat() if not df_15m.empty else None,
      "horizon_minutes": self.horizon,
      "timezone": self.tz,
      "prediction_schedule": "every :00, :15, :30, :45 ET",
      "last_error": self.last_error,
      "scheduler_running": self._scheduler is not None and getattr(self._scheduler, "running", False),
    }

  def _schedule_predictions(self, scheduler) -> None:
    scheduler.add_job(
      self.run_prediction,
      CronTrigger(minute="0,15,30,45", timezone=self.tz),
      id="predict",
      max_instances=1,
    )

  def start_background(self) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=self.tz)
    scheduler.add_job(self.fetch_and_store, "interval", minutes=1, id="fetch", max_instances=1)
    scheduler.add_job(self.resolve_outcomes, "interval", minutes=1, id="resolve", max_instances=1)
    self._schedule_predictions(scheduler)
    scheduler.add_job(self.fetch_and_store, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=2), id="fetch_now")
    scheduler.add_job(self.run_prediction, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=8), id="predict_now")
    scheduler.start()
    self._scheduler = scheduler
    log.info("Scheduler started: 15m slots at :00/:15/:30/:45 ET (%s)", self.tz)
    return scheduler

  def start_blocking(self) -> None:
    self.fetch_and_store()
    self.run_prediction()

    scheduler = BlockingScheduler(timezone=self.tz)
    scheduler.add_job(self.fetch_and_store, "interval", minutes=1, id="fetch")
    scheduler.add_job(self.resolve_outcomes, "interval", minutes=1, id="resolve")
    self._schedule_predictions(scheduler)
    try:
      scheduler.start()
    except (KeyboardInterrupt, SystemExit):
      log.info("Scheduler stopped")


def run_once(cfg: dict | None = None, model_path: str | None = None) -> Prediction | None:
  loop = PredictionLoop(cfg, model_path)
  loop.fetch_and_store()
  return loop.run_prediction()
