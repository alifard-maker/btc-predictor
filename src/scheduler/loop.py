from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.calibration.sources import KALSHI_EXIT_SOURCE, KALSHI_REF_SOURCE
from src.calibration.hourly_tracker import HourlyCalibrationTracker
from src.calibration.tracker import CalibrationTracker
from src.config import ensure_dirs, load_config
from src.data.fetcher import DataFetcher
from src.data.kalshi import KalshiClient, KalshiPriceQuote
from src.data.storage import CandleStorage, HistoricalCollector
from src.db.store import PredictionResolution
from src.features.slots import current_slot_start, floor_to_15m, slot_end
from src.logging.prediction_log import PredictionLogger
from src.logging.postmortem_log import PostmortemLogger
from src.models.predictor import Prediction, Predictor
from src.trading.exit_advisor import ExitAdvisor, SlotMonitor

log = logging.getLogger(__name__)


class PredictionLoop:
  """Fetch data every minute; predict at :00, :15, :30, :45 for next 15m slot."""

  def __init__(self, cfg: dict[str, Any] | None = None, model_path: str | None = None):
    self.cfg = cfg or load_config()
    ensure_dirs(self.cfg)

    self.fetcher = DataFetcher(self.cfg)
    self.kalshi = KalshiClient(self.cfg)
    self.storage = CandleStorage(self.cfg)
    self.predictor = Predictor(self.cfg, model_path=model_path or self._default_model_path())
    self.logger = PredictionLogger(self.cfg)
    self.postmortems = PostmortemLogger(self.cfg)
    self.calibration = CalibrationTracker(self.cfg)
    self.hourly_calibration = HourlyCalibrationTracker(self.cfg)
    self.tz = self.cfg.get("timezone", "America/New_York")
    self.horizon = self.cfg.get("prediction_horizon_minutes", 15)
    self.min_candles = self.cfg.get("min_candles_15m", 48)
    self.fetch_15m_count = self.cfg.get("fetch_candles_15m", 64)
    self.latest_prediction: Prediction | None = None
    self.exit_advisor = ExitAdvisor(self.cfg)
    self.last_error: str | None = None
    self._scheduler: BackgroundScheduler | BlockingScheduler | None = None
    self._ticker_cache: tuple[KalshiPriceQuote | None, float] | None = None
    self._slot_tick_cache: dict[str, dict[str, Any]] = {}
    self._late_entry_logged: set[str] = set()
    self._flip_logged: set[str] = set()
    self.train_status: dict[str, Any] = {"state": "idle"}
    self.hourly_train_status: dict[str, Any] = {"state": "idle"}
    self._hourly_predictor = None
    self.latest_hourly_prediction: dict[str, Any] | None = None

  def hourly_predictor(self):
    if self._hourly_predictor is None:
      from src.models.hourly_predictor import HourlyPredictor
      self._hourly_predictor = HourlyPredictor(self.cfg)
    return self._hourly_predictor

  def _ohlc_1h(self) -> pd.DataFrame:
    """Native 1h candles, falling back to resampled 15m."""
    df_1h = self.storage.load("1h")
    if not df_1h.empty and len(df_1h) >= 24:
      return df_1h
    df_15m = self.storage.load("15m")
    if df_15m.empty:
      return pd.DataFrame()
    df = df_15m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    agg = df.resample("1h").agg({
      "open": "first",
      "high": "max",
      "low": "min",
      "close": "last",
      "volume": "sum",
    }).dropna(subset=["close"])
    agg = agg.reset_index()
    return agg

  def daily_prediction(self) -> dict[str, Any]:
    if not self.cfg.get("daily", {}).get("enabled", True):
      return {"ok": False, "error": "Daily predictions disabled"}
    quote = self.live_price_quote(fresh=True)
    price = quote.price if quote else None
    if price is None:
      df_1m = self.storage.load("1m")
      if not df_1m.empty:
        price = float(df_1m["close"].iloc[-1])
    if price is None or price <= 0:
      return {"ok": False, "error": "Live BRTI unavailable"}
    df_1h = self._ohlc_1h()
    df_15m = self.storage.load("15m")
    out = self.hourly_predictor().predict(
      current_price=float(price),
      df_1h=df_1h,
      df_15m=df_15m if not df_15m.empty else None,
      calibration_tracker=self.calibration,
    )
    if quote:
      out["brti_live"] = round(quote.price, 2)
      out["brti_source"] = quote.source
    out["timezone"] = self.tz
    self.latest_hourly_prediction = out if out.get("ok") else None
    return out

  def run_hourly_prediction(self) -> dict[str, Any] | None:
    if not self.cfg.get("hourly", {}).get("enabled", True):
      return None
    try:
      self.resolve_hourly_outcomes()
      out = self.daily_prediction()
      if not out.get("ok"):
        return out
      row = self.hourly_predictor().to_log_row(out)
      if row.get("event_ticker"):
        self.hourly_calibration.log_prediction(row)
        log.info(
          "Hourly prediction logged: %s %s %s",
          row["event_ticker"],
          row.get("primary_signal"),
          row.get("primary_label"),
        )
      return out
    except Exception as e:
      log.exception("Hourly prediction failed: %s", e)
      self.last_error = str(e)
      return None

  def resolve_hourly_outcomes(self) -> None:
    from src.data.kalshi_hourly import try_resolve_pending

    pending = self.hourly_calibration.get_pending()
    if not pending:
      return
    resolved = 0
    for row in pending:
      res = try_resolve_pending(self.kalshi, row)
      if res is None:
        continue
      if self.hourly_calibration.resolve(str(row["event_ticker"]), res):
        resolved += 1
    if resolved:
      log.info("Resolved %d hourly predictions via Kalshi", resolved)
      self.refit_hourly_calibrator()
      self.calibrate_hourly_sigma()

  def refit_hourly_calibrator(self) -> bool:
    hp = self.hourly_predictor()
    if hp.calibrator is None:
      return False
    if self.hourly_calibration.fit_calibrator(hp.calibrator):
      from src.models.hourly_trainer import HourlyModelTrainer
      trainer = HourlyModelTrainer(self.cfg)
      trainer.model = hp.model
      trainer.feature_names = hp.feature_names
      trainer.calibrator = hp.calibrator
      path = Path(self.cfg["paths"]["models"]) / "model_hourly.joblib"
      if path.exists() and hp.model is not None:
        trainer.save(path)
      log.info("Hourly calibrator refit from resolved events")
      return True
    return False

  def calibrate_hourly_sigma(self) -> None:
    if not self.cfg.get("hourly", {}).get("sigma_calibration", True):
      return
    df = self.hourly_calibration.load_resolved()
    if len(df) < 10:
      return
    err = (pd.to_numeric(df["settle_brti"], errors="coerce") - pd.to_numeric(df["blended_mu"], errors="coerce")).abs()
    sigma = pd.to_numeric(df["terminal_sigma"], errors="coerce").replace(0, np.nan)
    ratio = float((err / sigma).median())
    if ratio > 0 and not np.isnan(ratio):
      hp = self.hourly_predictor()
      new_scale = max(0.5, min(2.0, hp._sigma_scale * ratio))
      hp.save_sigma_scale(new_scale)
      log.info("Hourly sigma scale updated to %.3f", new_scale)

  def train_hourly_model(self, min_samples: int | None = None) -> None:
    from src.models.hourly_trainer import HourlyModelTrainer

    self.hourly_train_status = {
      "state": "running",
      "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
      cfg = self.cfg
      if min_samples is not None:
        cfg = {**self.cfg, "hourly": {**self.cfg.get("hourly", {}), "min_train_samples": min_samples}}
      df_1h = self._ohlc_1h()
      df_15m = self.storage.load("15m")
      if df_1h.empty:
        raise ValueError("No 1h candle data — enable 1h fetch in config")
      trainer = HourlyModelTrainer(cfg)
      metrics = trainer.train(df_1h, df_15m if not df_15m.empty else None)
      model_path = Path(self.cfg["paths"]["models"]) / "model_hourly.joblib"
      trainer.save(model_path)
      self._hourly_predictor = None
      self.hourly_train_status = {
        "state": "done",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_path": str(model_path),
        "metrics": metrics,
        "candles_1h": len(df_1h),
      }
      log.info("Hourly model training complete: %s", metrics)
    except Exception as e:
      log.exception("Hourly model training failed")
      self.hourly_train_status = {
        "state": "error",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": str(e),
      }


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

    try:
      count_1h = int(self.cfg.get("hourly", {}).get("fetch_candles_1h", 168))
      df_1h = self.fetcher.fetch_latest_candles("1h", count=count_1h)
      if not df_1h.empty:
        self.storage.save("1h", df_1h)
    except Exception as e:
      log.warning("Failed to fetch 1h: %s", e)

  def resolve_outcomes(self) -> None:
    self.resolve_hourly_outcomes()
    pending = self.calibration.get_pending()
    if not pending:
      return

    price_lookup: dict[str, PredictionResolution] = {}
    for _row_id, ts_str, entry_price in pending:
      slot_s = floor_to_15m(pd.Timestamp(ts_str), self.tz)
      settlement = self.kalshi.slot_settlement(slot_s)
      if settlement is None or not settlement.settled:
        continue

      resolution = self.kalshi.resolution_for_entry(float(entry_price), settlement)
      if resolution is None:
        continue

      exit_price, actual_return, outcome = resolution
      price_lookup[ts_str] = PredictionResolution(
        exit_price=exit_price,
        actual_return=actual_return,
        exit_source=KALSHI_EXIT_SOURCE,
        outcome=outcome,
        reference_price=settlement.open_brti,
        reference_source=KALSHI_REF_SOURCE,
        kalshi_market_ticker=settlement.market_ticker,
      )

    if price_lookup:
      resolved = self.calibration.resolve_with_prices(price_lookup)
      log.info("Resolved %d predictions via Kalshi BRTI", resolved)
      self._log_postmortems(price_lookup.keys())

  def _log_postmortems(self, timestamps: Any) -> None:
    try:
      df = self.calibration.store.load_resolved()
      if df.empty:
        return
      ts_set = {pd.Timestamp(t, utc=True).isoformat() for t in timestamps}
      for _, row in df.iterrows():
        ts_key = pd.Timestamp(row["timestamp"], utc=True).isoformat()
        if ts_key in ts_set:
          self.postmortems.log_row(row.to_dict())
      self.refit_calibrator()
    except Exception as e:
      log.warning("Postmortem logging failed: %s", e)

  def collect_auxiliary(self) -> None:
    try:
      collector = HistoricalCollector(self.cfg)
      counts = collector.collect_auxiliary()
      log.info("Auxiliary data refreshed: %s", counts)
    except Exception as e:
      log.warning("Auxiliary collect failed: %s", e)

  def train_model(self, min_samples: int | None = None) -> None:
    """Train LightGBM in-process (intended for background thread)."""
    from src.models.trainer import ModelTrainer

    self.train_status = {
      "state": "running",
      "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
      cfg = self.cfg
      if min_samples is not None:
        cfg = {**self.cfg, "model": {**self.cfg.get("model", {}), "min_train_samples": min_samples}}

      storage = CandleStorage(self.cfg)
      df_15m = storage.load("15m")
      df_1m = storage.load("1m")
      if df_15m.empty:
        raise ValueError("No 15m candle data — run collect first")

      trainer = ModelTrainer(cfg)
      metrics = trainer.train(df_15m, df_1m if not df_1m.empty else None)
      model_path = Path(self.cfg["paths"]["models"]) / "model.joblib"
      trainer.save(model_path)
      self.predictor.load_model(str(model_path))
      self.train_status = {
        "state": "done",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_path": str(model_path),
        "metrics": metrics,
        "candles_15m": len(df_15m),
        "candles_1m": len(df_1m),
      }
      log.info("Model training complete: %s", metrics)
    except Exception as e:
      log.exception("Model training failed")
      self.train_status = {
        "state": "error",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": str(e),
      }

  def auto_retrain(self) -> None:
    """Daily scheduled retrain — runs in background."""
    acfg = self.cfg.get("auto_train", {})
    if not acfg.get("enabled", True):
      return
    if self.train_status.get("state") == "running":
      log.warning("Auto-retrain skipped: training already in progress")
      return
    log.info("Daily auto-retrain starting")
    threading.Thread(target=self.train_model, daemon=True).start()
    if self.cfg.get("hourly", {}).get("enabled", True):
      threading.Thread(target=self.train_hourly_model, daemon=True).start()

  def _schedule_hourly(self, scheduler) -> None:
    if not self.cfg.get("hourly", {}).get("enabled", True):
      return
    minute = int(self.cfg.get("hourly", {}).get("log_minute", 5))
    scheduler.add_job(
      self.run_hourly_prediction,
      CronTrigger(minute=str(minute), timezone=self.tz),
      id="hourly_predict",
      max_instances=1,
    )
    scheduler.add_job(
      self.refit_hourly_calibrator,
      "interval",
      hours=6,
      id="refit_hourly_calibrator",
      max_instances=1,
    )

  def _auto_train_first_run(self) -> datetime:
    """Next calendar day at configured hour (default 2:00 AM ET)."""
    tz = ZoneInfo(self.tz)
    now = datetime.now(tz)
    nxt = now.date() + timedelta(days=1)
    acfg = self.cfg.get("auto_train", {})
    hour = int(acfg.get("hour", 2))
    minute = int(acfg.get("minute", 0))
    return datetime(nxt.year, nxt.month, nxt.day, hour, minute, 0, tzinfo=tz)

  def reset_calibration_stats(self, *, note: str = "") -> dict[str, Any]:
    stats = self.calibration.reset_stats(note=note)
    self.latest_prediction = None
    self._late_entry_logged.clear()
    self._flip_logged.clear()
    log.info("Calibration stats reset (epoch archived): %s", stats)
    return stats

  def refit_calibrator(self) -> bool:
    if self.predictor.model is None:
      return False
    from src.models.trainer import ModelTrainer
    trainer = ModelTrainer(self.cfg)
    trainer.model = self.predictor.model
    trainer.feature_names = self.predictor.feature_names
    if trainer.fit_calibrator_from_tracker(self.calibration):
      self.predictor.calibrator = trainer.calibrator
      model_path = Path(self.cfg["paths"]["models"]) / "model.joblib"
      if model_path.exists():
        trainer.save(model_path)
      log.info("Probability calibrator refit from %d resolved slots", len(self.calibration.load_resolved()))
      return True
    return False

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

      slot_s = floor_to_15m(pd.Timestamp(datetime.now(timezone.utc)), self.tz)
      kalshi_ref = self._resolve_kalshi_t0(slot_s)
      open_quote = self.live_price_quote(fresh=True)
      current_quote = self.live_price_quote(fresh=True)
      locked = self._locked_slot_reference(slot_s)
      locked_ref = kalshi_ref
      if locked_ref is None and locked:
        locked_ref = float(locked["price"])

      pred = self.predictor.predict(
        df_15m,
        df_1m if not df_1m.empty else None,
        live_price=kalshi_ref,
        current_price=current_quote.price if current_quote else None,
        locked_reference=locked_ref,
        live_trade_time=open_quote.trade_time if open_quote else None,
        current_trade_time=current_quote.trade_time if current_quote else None,
        kalshi_reference=kalshi_ref,
      )
      self._remember_slot_reference(pred, open_quote, kalshi_ref)
      active = self.kalshi.active_btc15m_market()
      kalshi_ticker = active.market_ticker if active else ""
      self.logger.log(pred, kalshi_market_ticker=kalshi_ticker)
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

  def _resolve_kalshi_t0(self, slot_s: pd.Timestamp, *, retries: int = 6, delay_sec: float = 0.5) -> float | None:
    """Kalshi floor_strike at slot open — retry briefly while market row populates."""
    for attempt in range(retries):
      ref, _ = self.kalshi.slot_t0_reference(slot_s, fresh=True)
      if ref is not None and ref > 0:
        return float(ref)
      if attempt < retries - 1:
        time.sleep(delay_sec)
    log.warning("Kalshi floor_strike unavailable for slot %s after %d tries", slot_s, retries)
    return None

  def _live_cache_sec(self) -> float:
    return float(self.cfg.get("kalshi", {}).get("brti_cache_sec", 0))

  def _live_fallback_enabled(self) -> bool:
    return bool(self.cfg.get("kalshi", {}).get("live_fallback_exchange", True))

  def _exchange_tick_cache_sec(self) -> float:
    return float(self.cfg.get("kalshi", {}).get("exchange_tick_cache_sec", 1.0))

  def _exchange_live_quote(self, *, fresh: bool = True) -> KalshiPriceQuote | None:
    """Fresh exchange last trade — used when BRTI auth is missing or stale."""
    now_mono = time.monotonic()
    cache_sec = self._exchange_tick_cache_sec()
    if not fresh and self._ticker_cache and (now_mono - self._ticker_cache[1]) < cache_sec:
      return self._ticker_cache[0]
    try:
      ticker = self.fetcher.fetch_ticker_quote()
      trade_time = ticker.trade_time
      if trade_time is None:
        trade_time = datetime.now(timezone.utc)
      elif trade_time.tzinfo is None:
        trade_time = trade_time.replace(tzinfo=timezone.utc)
      source = "exchange_live" if not self.kalshi.authenticated else "exchange_fallback"
      quote = KalshiPriceQuote(price=ticker.price, source=source, trade_time=trade_time)
      self._ticker_cache = (quote, now_mono)
      return quote
    except Exception as e:
      log.warning("Exchange live tick failed: %s", e)
      if self._ticker_cache:
        return self._ticker_cache[0]
      return None

  def _live_quote(self, *, fresh: bool = False) -> KalshiPriceQuote | None:
    """Kalshi BRTI live price for P&L and display."""
    return self.kalshi.live_quote(fresh=fresh)

  def live_price_quote(self, *, fresh: bool = True) -> KalshiPriceQuote | None:
    """BRTI when authed; otherwise always a fresh exchange tick (never static t=0)."""
    max_stale = float(self.cfg.get("kalshi", {}).get("brti_max_stale_sec", 5))
    if self.kalshi.authenticated:
      brti = self.kalshi.fetch_brti_live(fresh=True)
      if brti is not None:
        return brti
      last = self.kalshi.last_brti_quote()
      if last is not None and last.age_sec is not None and last.age_sec <= max_stale:
        return last
    if self._live_fallback_enabled():
      return self._exchange_live_quote(fresh=fresh)
    return None

  def _live_price(self, max_age_sec: float | None = None) -> float | None:
    fresh = max_age_sec is not None and max_age_sec <= 0
    quote = self._live_quote(fresh=fresh)
    return quote.price if quote else None

  def _slot_cache_key(self, slot_s: pd.Timestamp) -> str:
    return floor_to_15m(slot_s, self.tz).isoformat()

  def _remember_slot_reference(
    self,
    pred: Prediction,
    quote: KalshiPriceQuote | None,
    kalshi_ref: float | None,
  ) -> None:
    if pred.slot_start is None:
      return
    key = self._slot_cache_key(pred.slot_start)
    self._slot_tick_cache[key] = {
      "price": pred.reference_price,
      "source": pred.reference_source,
      "trade_time": pred.reference_trade_time or (quote.trade_time.isoformat() if quote and quote.trade_time else None),
    }
    if kalshi_ref:
      self.kalshi._slot_targets[key] = float(kalshi_ref)

  def _locked_slot_reference(self, slot_s: pd.Timestamp) -> dict[str, Any] | None:
    return self._slot_tick_cache.get(self._slot_cache_key(slot_s))

  def _prediction_for_current_slot(self) -> dict[str, Any] | None:
    """DB/logged prediction for the slot that is active right now."""
    slot_s = current_slot_start(tz_name=self.tz)
    slot_key = floor_to_15m(slot_s, self.tz)

    if self.latest_prediction and self.latest_prediction.slot_start is not None:
      if floor_to_15m(self.latest_prediction.slot_start, self.tz) == slot_key:
        p = self.latest_prediction
        return {
          "timestamp": p.timestamp.isoformat(),
          "price": p.reference_price or p.price,
          "reference_price": p.reference_price or p.price,
          "reference_source": p.reference_source,
          "signal": p.signal.value,
          "prob_up": p.prob_up,
          "flip_signal": "",
        }

    try:
      df = self.calibration.load_recent(12)
      if df.empty:
        return None
      df = df.copy()
      df["_slot"] = pd.to_datetime(df["timestamp"], utc=True).apply(
        lambda t: floor_to_15m(t, self.tz)
      )
      match = df[df["_slot"] == slot_key]
      if match.empty:
        return None
      row = match.iloc[0]
      return {
        "timestamp": row["timestamp"],
        "price": float(row.get("price", 0)),
        "reference_price": float(row.get("price", 0)),
        "signal": str(row.get("signal", "NO TRADE")),
        "prob_up": float(row.get("prob_up", 0.5)),
        "flip_signal": str(row.get("flip_signal") or ""),
      }
    except Exception as e:
      log.warning("Could not load slot prediction: %s", e)
      return None

  def slot_monitor(self, reference_override: float | None = None) -> SlotMonitor:
    """Live hold / take-profit / cut-loss guidance for the active 15m window."""
    now = pd.Timestamp(datetime.now(timezone.utc))
    slot_s = current_slot_start(now, self.tz)
    df_1m = self.storage.load("1m")

    pred = self._prediction_for_current_slot()
    live_quote = self.live_price_quote(fresh=True)
    current = live_quote.price if live_quote else None

    api_ref, ref_source = self.kalshi.slot_t0_reference(slot_s, fresh=True)
    if api_ref is None and pred is not None:
      api_ref = float(pred.get("reference_price") or pred.get("price") or 0)
      ref_source = str(pred.get("reference_source") or "kalshi_brti_target")
    elif api_ref is None:
      locked = self._locked_slot_reference(slot_s)
      if locked:
        api_ref = float(locked["price"])
        ref_source = str(locked.get("source") or "kalshi_brti_target")

    if api_ref is None:
      api_ref = 0.0
      ref_source = ref_source or "unavailable"

    effective_ref = api_ref
    using_override = False
    if reference_override is not None and reference_override > 0:
      effective_ref = float(reference_override)
      using_override = True

    original_prob = float(pred.get("prob_up", 0.5)) if pred else 0.5
    existing_flip = str(pred.get("flip_signal") or "") if pred else ""

    if pred is None:
      monitor = self.exit_advisor.evaluate(
        now=now,
        reference_price=effective_ref,
        current_price=current if current is not None else effective_ref,
        signal_at_open="NO TRADE",
        df_1m=df_1m if not df_1m.empty else None,
        slot_start=slot_s,
        original_prob_up=original_prob,
        existing_flip=existing_flip,
      )
    else:
      monitor = self.exit_advisor.evaluate(
        now=now,
        reference_price=effective_ref,
        current_price=current if current is not None else effective_ref,
        signal_at_open=str(pred.get("signal", "NO TRADE")),
        df_1m=df_1m if not df_1m.empty else None,
        slot_start=slot_s,
        original_prob_up=original_prob,
        existing_flip=existing_flip,
      )

    monitor.reference_price_api = api_ref if api_ref else None
    monitor.using_override = using_override
    monitor.reference_source = ref_source if not using_override else "user_override"
    monitor.current_price_source = live_quote.source if live_quote else "unavailable"
    monitor.current_price_as_of = (
      live_quote.trade_time.isoformat() if live_quote and live_quote.trade_time else None
    )
    monitor.live_price_age_sec = round(live_quote.age_sec, 1) if live_quote and live_quote.age_sec is not None else None
    monitor.kalshi = self.kalshi.active_market_summary()
    self._maybe_log_late_entry(slot_s, monitor)
    self._maybe_log_flip(slot_s, monitor)
    return monitor

  def _maybe_log_flip(self, slot_s: pd.Timestamp, monitor) -> None:
    action = monitor.action.value if hasattr(monitor.action, "value") else str(monitor.action)
    if action not in ("FLIP LONG", "FLIP SHORT"):
      return
    key = self._slot_cache_key(slot_s)
    if key in self._flip_logged:
      return
    prob = monitor.reassessed_prob_up
    if prob is None:
      return
    ts = floor_to_15m(slot_s, self.tz).isoformat()
    if self.calibration.record_flip(ts, action, float(prob), int(monitor.seconds_remaining)):
      self._flip_logged.add(key)
      log.info("Flip logged: %s %.0f%% UP (%ds left)", action, prob * 100, monitor.seconds_remaining)

  def _maybe_log_late_entry(self, slot_s: pd.Timestamp, monitor) -> None:
    action = getattr(monitor, "late_entry_action", "") or ""
    if action not in ("LATE LONG", "LATE SHORT"):
      return
    key = self._slot_cache_key(slot_s)
    if key in self._late_entry_logged:
      return
    prob = monitor.reassessed_prob_up
    if prob is None:
      return
    ts = floor_to_15m(slot_s, self.tz).isoformat()
    if self.calibration.record_late_entry(ts, action, float(prob), int(monitor.seconds_remaining)):
      self._late_entry_logged.add(key)
      log.info("Late entry logged: %s %.0f%% UP (%ds left)", action, prob * 100, monitor.seconds_remaining)

  def poll_brti(self) -> None:
    """Background refresh of live price (BRTI or exchange)."""
    self.live_price_quote(fresh=True)

  def status(self) -> dict[str, Any]:
    df_15m = self.storage.load("15m")
    df_1m = self.storage.load("1m")
    live = self.live_price_quote(fresh=True)
    live_tick: dict[str, Any] | None = None
    if live:
      live_tick = {
        "price": round(live.price, 2),
        "source": live.source,
        "age_sec": round(live.age_sec, 1) if live.age_sec is not None else None,
      }
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
      "price_feed": self.kalshi.price_feed_label(),
      "settlement_reference": self.kalshi.settlement_reference_label(),
      "live_tick": live_tick,
      "kalshi": self.kalshi.status(),
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
    self._schedule_hourly(scheduler)
    scheduler.add_job(self.fetch_and_store, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=2), id="fetch_now")
    scheduler.add_job(self.run_prediction, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=8), id="predict_now")
    scheduler.add_job(self.run_hourly_prediction, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=15), id="hourly_now")
    scheduler.add_job(self.collect_auxiliary, "interval", hours=6, id="auxiliary", max_instances=1)
    scheduler.add_job(self.collect_auxiliary, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=12), id="auxiliary_now")
    scheduler.add_job(self.refit_calibrator, "interval", hours=6, id="refit_calibrator", max_instances=1)
    acfg = self.cfg.get("auto_train", {})
    if acfg.get("enabled", True):
      first = self._auto_train_first_run()
      scheduler.add_job(
        self.auto_retrain,
        CronTrigger(
          hour=int(acfg.get("hour", 2)),
          minute=int(acfg.get("minute", 0)),
          timezone=self.tz,
          start_date=first,
        ),
        id="auto_train",
        max_instances=1,
      )
      log.info("Auto-train scheduled daily at %02d:%02d %s from %s", int(acfg.get("hour", 2)), int(acfg.get("minute", 0)), self.tz, first.isoformat())
    poll_sec = float(self.cfg.get("kalshi", {}).get("brti_poll_sec", 1))
    scheduler.add_job(self.poll_brti, "interval", seconds=poll_sec, id="brti_poll", max_instances=1)
    scheduler.add_job(self.poll_brti, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=1), id="brti_now")
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
