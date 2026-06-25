from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from src.calibration.tracker import CalibrationTracker
from src.config import ensure_dirs, load_config
from src.data.fetcher import DataFetcher
from src.data.storage import CandleStorage
from src.logging.prediction_log import PredictionLogger
from src.models.predictor import Prediction, Predictor

log = logging.getLogger(__name__)


class PredictionLoop:
  """Fetch data every minute, predict every 5 minutes, resolve outcomes."""

  def __init__(self, cfg: dict[str, Any] | None = None, model_path: str | None = None):
    self.cfg = cfg or load_config()
    ensure_dirs(self.cfg)

    self.fetcher = DataFetcher(self.cfg)
    self.storage = CandleStorage(self.cfg)
    self.predictor = Predictor(self.cfg, model_path=model_path or self._default_model_path())
    self.logger = PredictionLogger(self.cfg)
    self.calibration = CalibrationTracker(self.cfg)
    self.horizon = self.cfg.get("prediction_horizon_minutes", 5)
    self.latest_prediction: Prediction | None = None
    self.last_error: str | None = None
    self._scheduler: BackgroundScheduler | BlockingScheduler | None = None

  def _default_model_path(self) -> str | None:
    p = Path(self.cfg["paths"]["models"]) / "model.joblib"
    return str(p) if p.exists() else None

  def fetch_and_store(self) -> None:
    for interval in ["1m", "15m"]:
      try:
        df = self.fetcher.fetch_latest_candles(interval, count=500)
        if not df.empty:
          self.storage.save(interval, df)
      except Exception as e:
        log.warning("Failed to fetch %s: %s", interval, e)
        self.last_error = str(e)

  def resolve_outcomes(self) -> None:
    df_1m = self.storage.load("1m")
    if df_1m.empty:
      return

    pending = self.calibration.get_pending()
    price_lookup = {}
    for _row_id, ts_str, entry_price in pending:
      ts = pd.Timestamp(ts_str)
      exit_ts = ts + timedelta(minutes=self.horizon)
      future = df_1m[df_1m["timestamp"] >= exit_ts]
      if future.empty:
        continue
      exit_price = float(future.iloc[0]["close"])
      actual_return = (exit_price - entry_price) / entry_price
      price_lookup[ts_str] = (exit_price, actual_return)

    if price_lookup:
      resolved = self.calibration.resolve_with_prices(price_lookup)
      log.info("Resolved %d predictions", resolved)

  def run_prediction(self) -> Prediction | None:
    try:
      self.resolve_outcomes()
      self.fetch_and_store()

      df_1m = self.storage.load("1m")
      df_15m = self.storage.load("15m")

      if df_1m.empty or len(df_1m) < 100:
        self.fetch_and_store()
        df_1m = self.storage.load("1m")
        df_15m = self.storage.load("15m")

      if df_1m.empty:
        self.last_error = "No candle data available"
        log.error(self.last_error)
        return None

      pred = self.predictor.predict(df_1m, df_15m)
      self.logger.log(pred)
      self.latest_prediction = pred
      self.last_error = None
      log.info(
        "Prediction: UP=%.1f%% signal=%s price=$%.2f",
        pred.prob_up * 100, pred.signal.value, pred.price,
      )
      return pred
    except Exception as e:
      self.last_error = str(e)
      log.exception("Prediction failed: %s", e)
      return None

  def status(self) -> dict[str, Any]:
    df_1m = self.storage.load("1m")
    return {
      "symbol": self.cfg["symbol"],
      "exchange": getattr(self.fetcher, "_exchange_id", self.cfg.get("exchange")),
      "model": "trained" if self.predictor.model else "baseline",
      "candles_1m": len(df_1m),
      "latest_candle": df_1m["timestamp"].iloc[-1].isoformat() if not df_1m.empty else None,
      "horizon_minutes": self.horizon,
      "last_error": self.last_error,
      "scheduler_running": self._scheduler is not None and getattr(self._scheduler, "running", False),
    }

  def start_background(self) -> BackgroundScheduler:
    """Non-blocking scheduler for FastAPI / Railway."""
    interval = self.cfg.get("prediction_interval_minutes", 5)

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(self.fetch_and_store, "interval", minutes=1, id="fetch", max_instances=1)
    scheduler.add_job(self.run_prediction, "interval", minutes=interval, id="predict", max_instances=1)
    scheduler.add_job(self.resolve_outcomes, "interval", minutes=1, id="resolve", max_instances=1)
    # Run first fetch/predict immediately in background — don't block API startup
    scheduler.add_job(self.fetch_and_store, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=2), id="fetch_now")
    scheduler.add_job(self.run_prediction, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=5), id="predict_now")
    scheduler.start()
    self._scheduler = scheduler
    log.info("Background scheduler started (predict every %dm)", interval)
    return scheduler

  def start_blocking(self) -> None:
    """CLI mode — blocks until interrupted."""
    interval = self.cfg.get("prediction_interval_minutes", 5)
    self.fetch_and_store()
    self.run_prediction()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(self.fetch_and_store, "interval", minutes=1, id="fetch")
    scheduler.add_job(self.run_prediction, "interval", minutes=interval, id="predict")
    scheduler.add_job(self.resolve_outcomes, "interval", minutes=1, id="resolve")
    try:
      scheduler.start()
    except (KeyboardInterrupt, SystemExit):
      log.info("Scheduler stopped")


def run_once(cfg: dict | None = None, model_path: str | None = None) -> Prediction | None:
  loop = PredictionLoop(cfg, model_path)
  loop.fetch_and_store()
  return loop.run_prediction()
