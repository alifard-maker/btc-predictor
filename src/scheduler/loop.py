from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.calibration.tracker import CalibrationTracker
from src.config import ensure_dirs, load_config
from src.data.fetcher import DataFetcher, TickerQuote
from src.data.storage import CandleStorage
from src.features.slots import current_slot_start, floor_to_15m, reference_price_at_slot, slot_end
from src.logging.prediction_log import PredictionLogger
from src.models.predictor import Prediction, Predictor
from src.trading.exit_advisor import ExitAdvisor, SlotMonitor

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
    self.exit_advisor = ExitAdvisor(self.cfg)
    self.last_error: str | None = None
    self._scheduler: BackgroundScheduler | BlockingScheduler | None = None
    self._ticker_cache: tuple[TickerQuote, float] | None = None  # (quote, monotonic time)
    self._slot_tick_cache: dict[str, dict[str, Any]] = {}

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

      open_quote = self._live_quote(fresh=True)
      live_price = open_quote.price if open_quote else None
      current_quote = self._live_quote(fresh=True)
      locked = self._locked_slot_reference(floor_to_15m(pd.Timestamp(datetime.now(timezone.utc)), self.tz))
      locked_ref = float(locked["price"]) if locked else None

      pred = self.predictor.predict(
        df_15m,
        df_1m if not df_1m.empty else None,
        live_price=live_price,
        current_price=current_quote.price if current_quote else None,
        locked_reference=locked_ref,
        live_trade_time=open_quote.trade_time if open_quote else None,
        current_trade_time=current_quote.trade_time if current_quote else None,
      )
      self._remember_slot_reference(pred, open_quote)
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

  def _live_cache_sec(self) -> float:
    return float(self.cfg.get("price_feed", {}).get("live_cache_sec", 2))

  def _live_quote(self, *, fresh: bool = False) -> TickerQuote | None:
    """Real-time last trade; cached briefly unless fresh=True."""
    now = time.monotonic()
    cache_sec = self._live_cache_sec()
    if not fresh and self._ticker_cache and (now - self._ticker_cache[1]) < cache_sec:
      return self._ticker_cache[0]
    try:
      quote = self.fetcher.fetch_ticker_quote()
      self._ticker_cache = (quote, now)
      return quote
    except Exception as e:
      log.debug("Ticker fetch failed, using candle fallback: %s", e)

    df_1m = self.storage.load("1m")
    if not df_1m.empty:
      return TickerQuote(price=float(df_1m.iloc[-1]["close"]), source="1m_close")
    try:
      batch = self.fetcher.fetch_latest_candles("1m", count=1)
      if not batch.empty:
        return TickerQuote(price=float(batch.iloc[-1]["close"]), source="1m_close")
    except Exception:
      pass
    return None

  def _live_price(self, max_age_sec: float | None = None) -> float | None:
    fresh = max_age_sec is not None and max_age_sec <= 0
    quote = self._live_quote(fresh=fresh)
    return quote.price if quote else None

  def _slot_cache_key(self, slot_s: pd.Timestamp) -> str:
    return floor_to_15m(slot_s, self.tz).isoformat()

  def _remember_slot_reference(self, pred: Prediction, quote: TickerQuote | None) -> None:
    if pred.slot_start is None:
      return
    key = self._slot_cache_key(pred.slot_start)
    self._slot_tick_cache[key] = {
      "price": pred.reference_price,
      "source": pred.reference_source,
      "trade_time": pred.reference_trade_time or (quote.trade_time.isoformat() if quote and quote.trade_time else None),
    }

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
    live_quote = self._live_quote(fresh=True)
    current = live_quote.price if live_quote else None

    locked = self._locked_slot_reference(slot_s)
    if pred is not None:
      api_ref = float(pred.get("reference_price") or pred.get("price") or 0)
      ref_source = str(pred.get("reference_source") or (locked.get("source") if locked else "") or "")
    elif locked:
      api_ref = float(locked["price"])
      ref_source = str(locked.get("source") or "locked_tick")
    else:
      pf = self.cfg.get("price_feed", {})
      tick_window = float(pf.get("live_tick_window_sec", 120))
      ref = reference_price_at_slot(
        df_1m if not df_1m.empty else None,
        slot_s,
        live_price=current,
        now_utc=now,
        live_tick_window_sec=tick_window,
      )
      api_ref = ref.price
      ref_source = ref.source

    effective_ref = api_ref
    using_override = False
    if reference_override is not None and reference_override > 0:
      effective_ref = float(reference_override)
      using_override = True

    if current is None:
      current = effective_ref

    original_prob = float(pred.get("prob_up", 0.5)) if pred else 0.5

    if pred is None:
      monitor = self.exit_advisor.evaluate(
        now=now,
        reference_price=effective_ref,
        current_price=current,
        signal_at_open="NO TRADE",
        df_1m=df_1m if not df_1m.empty else None,
        slot_start=slot_s,
        original_prob_up=original_prob,
      )
    else:
      monitor = self.exit_advisor.evaluate(
        now=now,
        reference_price=effective_ref,
        current_price=current,
        signal_at_open=str(pred.get("signal", "NO TRADE")),
        df_1m=df_1m if not df_1m.empty else None,
        slot_start=slot_s,
        original_prob_up=original_prob,
      )

    monitor.reference_price_api = api_ref if api_ref else None
    monitor.using_override = using_override
    monitor.reference_source = ref_source if not using_override else "user_override"
    monitor.current_price_as_of = (
      live_quote.trade_time.isoformat() if live_quote and live_quote.trade_time else None
    )
    monitor.live_price_age_sec = round(live_quote.age_sec, 1) if live_quote and live_quote.age_sec is not None else None
    return monitor

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
      "price_feed": self.fetcher.price_feed_label(),
      "settlement_reference": self.fetcher.settlement_reference_label(),
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
