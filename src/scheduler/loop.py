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
from src.assets import asset_cfg, asset_enabled, index_id_for_cfg
from src.config import ensure_dirs, load_config
from src.data.fetcher import DataFetcher
from src.data.kalshi import KalshiClient, KalshiPriceQuote
from src.data.storage import CandleStorage, HistoricalCollector
from src.db.store import PredictionResolution
from src.features.slots import current_slot_start, floor_to_15m, slot_times_match, slot_end
from src.logging.prediction_log import PredictionLogger
from src.logging.postmortem_log import PostmortemLogger
from src.models.predictor import Prediction, Predictor
from src.trading.exit_advisor import ExitAdvisor, SlotMonitor
from src.trading.second_chance import SecondChanceAdvisor

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
    self._second_chance_logged: set[str] = set()
    self._second_chance_advisor: SecondChanceAdvisor | None = None
    self.train_status: dict[str, Any] = {"state": "idle"}
    self.hourly_train_status: dict[str, Any] = {"state": "idle"}
    self.second_chance_train_status: dict[str, Any] = {"state": "idle"}
    self.eth_train_status: dict[str, Any] = {"state": "idle"}
    self.eth_hourly_train_status: dict[str, Any] = {"state": "idle"}
    self.eth_second_chance_train_status: dict[str, Any] = {"state": "idle"}
    self._hourly_predictor = None
    self.latest_hourly_prediction: dict[str, Any] | None = None
    self._eth_cfg: dict[str, Any] | None = None
    self._eth_fetcher: DataFetcher | None = None
    self._eth_storage: CandleStorage | None = None
    self._eth_hourly_predictor = None
    self._eth_ticker_cache: tuple[KalshiPriceQuote | None, float] | None = None
    self.eth_hourly_calibration: HourlyCalibrationTracker | None = None
    self.latest_eth_hourly_prediction: dict[str, Any] | None = None
    self._eth_kalshi: KalshiClient | None = None
    self._eth_predictor: Predictor | None = None
    self.eth_calibration: CalibrationTracker | None = None
    self._eth_logger: PredictionLogger | None = None
    self.latest_eth_prediction: Prediction | None = None
    self._eth_slot_tick_cache: dict[str, dict[str, Any]] = {}
    self._eth_late_entry_logged: set[str] = set()
    self._eth_flip_logged: set[str] = set()
    self._eth_second_chance_logged: set[str] = set()
    self._eth_second_chance_advisor: SecondChanceAdvisor | None = None
    self.eth_last_error: str | None = None
    self._hourly_bot_stores: dict[str, Any] = {}
    self._hourly_bots: dict[str, Any] = {}
    self._slot15_bot_stores: dict[str, Any] = {}
    self._slot15_bots: dict[str, Any] = {}
    if asset_enabled(self.cfg, "eth"):
      self._eth_cfg = asset_cfg(self.cfg, "eth")
      ensure_dirs(self._eth_cfg)
      self.eth_hourly_calibration = HourlyCalibrationTracker(self._eth_cfg, asset="eth")
      if self._eth_cfg.get("kalshi", {}).get("enabled", True):
        self.eth_calibration = CalibrationTracker(self._eth_cfg)

  def _asset_hourly_calibration(self, asset: str) -> HourlyCalibrationTracker:
    if asset == "eth":
      if self.eth_hourly_calibration is None:
        raise RuntimeError("ETH hourly is disabled")
      return self.eth_hourly_calibration
    return self.hourly_calibration

  def _slot15m_enabled(self, asset: str) -> bool:
    if asset == "btc":
      return True
    if not asset_enabled(self.cfg, "eth") or self.eth_calibration is None:
      return False
    acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
    return bool(acfg.get("kalshi", {}).get("enabled", True))

  def _acfg_15m(self, asset: str) -> dict[str, Any]:
    return self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))

  def _kalshi_for(self, asset: str) -> KalshiClient:
    if asset == "btc":
      return self.kalshi
    if self._eth_kalshi is None:
      self._eth_kalshi = KalshiClient(self._eth_cfg or asset_cfg(self.cfg, "eth"))
    return self._eth_kalshi

  def _calibration_for(self, asset: str) -> CalibrationTracker:
    if asset == "btc":
      return self.calibration
    if self.eth_calibration is None:
      raise RuntimeError("ETH 15m is disabled")
    return self.eth_calibration

  def _predictor_for(self, asset: str) -> Predictor:
    if asset == "btc":
      return self.predictor
    if self._eth_predictor is None:
      eth_path = Path(self._acfg_15m("eth")["paths"]["models"]) / "model.joblib"
      self._eth_predictor = Predictor(
        self._eth_cfg or asset_cfg(self.cfg, "eth"),
        model_path=str(eth_path) if eth_path.exists() else None,
      )
    return self._eth_predictor

  def _logger_for(self, asset: str) -> PredictionLogger:
    if asset == "btc":
      return self.logger
    if self._eth_logger is None:
      self._eth_logger = PredictionLogger(self._eth_cfg or asset_cfg(self.cfg, "eth"))
    return self._eth_logger

  def _slot_state(self, asset: str) -> dict[str, Any]:
    if asset == "btc":
      return {
        "latest_prediction": self.latest_prediction,
        "slot_tick_cache": self._slot_tick_cache,
        "late_entry_logged": self._late_entry_logged,
        "flip_logged": self._flip_logged,
        "second_chance_logged": self._second_chance_logged,
        "last_error_attr": "last_error",
      }
    return {
      "latest_prediction": self.latest_eth_prediction,
      "slot_tick_cache": self._eth_slot_tick_cache,
      "late_entry_logged": self._eth_late_entry_logged,
      "flip_logged": self._eth_flip_logged,
      "second_chance_logged": self._eth_second_chance_logged,
      "last_error_attr": "eth_last_error",
    }

  def eth_second_chance_advisor(self) -> SecondChanceAdvisor:
    if self._eth_second_chance_advisor is None:
      self._eth_second_chance_advisor = SecondChanceAdvisor(self._eth_cfg or asset_cfg(self.cfg, "eth"))
    return self._eth_second_chance_advisor

  def eth_fetcher(self) -> DataFetcher:
    if self._eth_fetcher is None:
      self._eth_fetcher = DataFetcher(self._eth_cfg or asset_cfg(self.cfg, "eth"))
    return self._eth_fetcher

  def eth_storage(self) -> CandleStorage:
    if self._eth_storage is None:
      self._eth_storage = CandleStorage(self._eth_cfg or asset_cfg(self.cfg, "eth"))
    return self._eth_storage

  def eth_hourly_predictor(self):
    if self._eth_hourly_predictor is None:
      from src.models.hourly_predictor import HourlyPredictor
      self._eth_hourly_predictor = HourlyPredictor(self._eth_cfg or asset_cfg(self.cfg, "eth"), asset="eth")
    return self._eth_hourly_predictor

  def second_chance_advisor(self) -> SecondChanceAdvisor:
    if self._second_chance_advisor is None:
      self._second_chance_advisor = SecondChanceAdvisor(self.cfg)
    return self._second_chance_advisor

  def hourly_predictor(self):
    if self._hourly_predictor is None:
      from src.models.hourly_predictor import HourlyPredictor
      self._hourly_predictor = HourlyPredictor(self.cfg)
    return self._hourly_predictor

  def _ohlc_1h(self, *, storage: CandleStorage | None = None) -> pd.DataFrame:
    """Native 1h candles, falling back to resampled 15m."""
    store = storage or self.storage
    df_1h = store.load("1h")
    if not df_1h.empty and len(df_1h) >= 24:
      return df_1h
    df_15m = store.load("15m")
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
    return self._hourly_tab_prediction("btc")

  def hourly_bot_store(self, asset: str):
    asset = asset.lower()
    if asset not in self._hourly_bot_stores:
      from src.trading.hourly_bot_store import HourlyBotStore

      acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))
      logs = Path(acfg.get("paths", {}).get("logs", "data/logs"))
      self._hourly_bot_stores[asset] = HourlyBotStore(logs / f"hourly_bot_{asset}.db")
    return self._hourly_bot_stores[asset]

  def hourly_bot(self, asset: str):
    asset = asset.lower()
    if asset not in self._hourly_bots:
      from src.trading.hourly_bot import HourlyBot

      store = self.hourly_bot_store(asset)
      kalshi = self._kalshi_for(asset)
      self._hourly_bots[asset] = HourlyBot(store, kalshi_client=kalshi, asset=asset)
    return self._hourly_bots[asset]

  def eth_hourly_bot_store(self):
    return self.hourly_bot_store("eth")

  def eth_hourly_bot(self):
    return self.hourly_bot("eth")

  def hourly_bot_status(self, asset: str, tab: dict[str, Any] | None = None) -> dict[str, Any]:
    asset = asset.lower()
    if asset == "eth" and not asset_enabled(self.cfg, "eth"):
      return {"ok": False, "error": "ETH disabled"}
    event_ticker = None
    if tab:
      event_ticker = (tab.get("event") or {}).get("event_ticker")
    status = self.hourly_bot_store(asset).status(event_ticker)
    status["ok"] = True
    status["asset"] = asset
    store = self.hourly_bot_store(asset)
    status["recent_trades"] = store.list_trades(limit=100)
    status["hour_trades"] = (
      store.list_trades(limit=50, event_ticker=event_ticker) if event_ticker else []
    )
    open_pos = list(status.get("open_positions") or [])
    if tab and tab.get("ok"):
      from src.trading.hourly_bot import enrich_open_positions_live

      acfg = self._acfg_15m(asset)
      open_pos = enrich_open_positions_live(open_pos, tab, acfg)
      status["open_positions"] = open_pos
    hs = status.get("hourly_summary") or status.get("hour_summary")
    if hs:
      hs = dict(hs)
      realized = float(hs.get("realized_pnl_usd") or 0)
      unrealized = round(
        sum(float(p.get("unrealized_pnl_usd") or 0) for p in open_pos),
        2,
      )
      hs["unrealized_pnl_usd"] = unrealized
      hs["total_pnl_usd"] = round(realized + unrealized, 2)
      status["hourly_summary"] = hs
      status["hour_summary"] = hs
    kalshi = self._kalshi_for(asset)
    status["kalshi_authenticated"] = bool(kalshi and kalshi.authenticated)
    if tab and tab.get("ok"):
      live = tab.get("live") or tab
      primary = live.get("primary_pick") or {}
      regime = live.get("regime") or {}
      status["entry_watch"] = {
        "signal": primary.get("signal"),
        "label": primary.get("label"),
        "edge": primary.get("edge"),
        "regime_allow_trade": regime.get("allow_trade"),
        "regime_reasons": list(regime.get("reasons") or [])[:3],
      }
    status["auto_tuning"] = store.get_auto_tuning()
    return status

  def eth_hourly_bot_status(self, tab: dict[str, Any] | None = None) -> dict[str, Any]:
    return self.hourly_bot_status("eth", tab)

  def _maybe_run_hourly_bot(self, asset: str, tab: dict[str, Any], trigger: str) -> None:
    if not tab.get("ok"):
      return
    try:
      self.hourly_bot(asset).evaluate_from_tab(tab, trigger=trigger)
    except Exception as e:
      log.exception("%s hourly bot %s failed: %s", asset.upper(), trigger, e)

  def _maybe_run_eth_hourly_bot(self, tab: dict[str, Any], trigger: str) -> None:
    self._maybe_run_hourly_bot("eth", tab, trigger)

  def _run_hourly_bot_continuous(self, asset: str) -> None:
    asset = asset.lower()
    store = self.hourly_bot_store(asset)
    settings = store.get_settings()
    active = settings.enabled and settings.continuous
    try:
      if not active:
        if not settings.enabled:
          store.set_last_skip_reason("auto_bet_off")
        return
      acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))
      tab = self._hourly_tab_prediction(asset)
      if tab.get("ok"):
        self.hourly_bot(asset).run_continuous_cycle(tab, cfg=acfg)
    except Exception as e:
      log.exception("%s hourly bot continuous cycle failed: %s", asset.upper(), e)
    finally:
      store.record_cycle(active=active)

  def run_hourly_bot_continuous(self) -> None:
    self._run_hourly_bot_continuous("btc")

  def run_eth_hourly_bot_continuous(self) -> None:
    if not asset_enabled(self.cfg, "eth"):
      return
    self._run_hourly_bot_continuous("eth")

  def _run_hourly_bot_intrahour(self, asset: str) -> None:
    self._run_hourly_bot_continuous(asset)

  def run_hourly_bot_intrahour(self) -> None:
    self._run_hourly_bot_continuous("btc")

  def run_eth_hourly_bot_intrahour(self) -> None:
    if not asset_enabled(self.cfg, "eth"):
      return
    self._run_hourly_bot_continuous("eth")

  def slot15_bot_store(self, asset: str):
    asset = asset.lower()
    if asset not in self._slot15_bot_stores:
      from src.trading.slot15_bot_store import Slot15BotStore

      acfg = self._acfg_15m(asset)
      logs = Path(acfg.get("paths", {}).get("logs", "data/logs"))
      self._slot15_bot_stores[asset] = Slot15BotStore(logs / f"slot15_bot_{asset}.db")
    return self._slot15_bot_stores[asset]

  def slot15_bot(self, asset: str):
    asset = asset.lower()
    if asset not in self._slot15_bots:
      from src.trading.slot15_bot import Slot15Bot

      store = self.slot15_bot_store(asset)
      kalshi = self._kalshi_for(asset)
      self._slot15_bots[asset] = Slot15Bot(store, kalshi_client=kalshi, asset=asset)
    return self._slot15_bots[asset]

  def _slot_times_match(
    self,
    pred_slot: datetime | pd.Timestamp | None,
    monitor_slot_key: str | pd.Timestamp | None,
  ) -> bool:
    return slot_times_match(pred_slot, monitor_slot_key, self.tz)

  def _slot15_tab(self, asset: str, reference_override: float | None = None) -> dict[str, Any]:
    """Live 15m tab payload for bot evaluation."""
    asset = asset.lower()
    if asset == "eth" and not self._slot15m_enabled("eth"):
      return {"ok": False, "error": "ETH 15m disabled", "asset": asset}

    acfg = self._acfg_15m(asset)
    kalshi = self._kalshi_for(asset)
    monitor = self._slot_monitor_for_asset(asset, reference_override).to_dict()
    kalshi_summary = kalshi.active_market_summary()
    slot_key = monitor.get("slot_start")
    bot_cfg = (acfg.get("intra_slot") or {}).get("bot") or {}
    paper_max_spread_cents = int(bot_cfg.get("paper_max_spread_cents", 40))
    probe_raw = bot_cfg.get("probe_no_trade") or {}
    probe_no_trade = {
      "enabled": bool(probe_raw.get("enabled", True)),
      "min_prob": float(probe_raw.get("min_prob", 0.58)),
      "min_elapsed_pct": float(probe_raw.get("min_elapsed_pct", 7.0)),
    }

    state = self._slot_state(asset)
    pred_obj = state["latest_prediction"]
    pred_dict: dict[str, Any] | None = None
    bet_assessment: dict[str, Any] | None = None

    pred_matches_slot = self._slot_times_match(
      pred_obj.slot_start if pred_obj is not None else None,
      slot_key,
    )

    if pred_obj is not None and pred_matches_slot:
      from src.trading.slot15_bet_assessment import assess_slot15_from_prediction

      ref = pred_obj.reference_price or pred_obj.price
      pred_dict = {
        "signal": pred_obj.signal.value,
        "model_signal": pred_obj.model_signal,
        "prob_up": pred_obj.prob_up,
        "regime_notes": pred_obj.regime_notes or [],
        "reference_price": ref,
        "price": pred_obj.price,
        "expected_move": pred_obj.expected_move,
      }
      bet_assessment = assess_slot15_from_prediction(pred_obj, acfg)
    else:
      row = self._prediction_for_current_slot(asset=asset)
      if row:
        pred_dict = dict(row)
        from src.trading.slot15_bet_assessment import assess_slot15_bet

        ref = row.get("reference_price") or row.get("price")
        expected_move_pct = None
        bet_assessment = assess_slot15_bet(
          signal=str(row.get("signal", "NO TRADE")),
          model_signal=row.get("model_signal"),
          regime_allow_trade=True,
          prob_up=float(row.get("prob_up", 0.5)),
          expected_move_pct=expected_move_pct,
          min_confidence=float(acfg.get("min_edge_confidence", 0.57)),
          min_expected_move_pct=float((acfg.get("intra_slot") or {}).get("fee_buffer_pct", 0.08)),
        )

    ok = bool(slot_key and kalshi_summary and kalshi_summary.get("market_ticker"))
    return {
      "ok": ok,
      "asset": asset,
      "slot_key": slot_key,
      "slot_label": monitor.get("slot_label"),
      "prediction": pred_dict,
      "monitor": monitor,
      "kalshi": kalshi_summary,
      "bet_assessment": bet_assessment,
      "paper_max_spread_cents": paper_max_spread_cents,
      "probe_no_trade": probe_no_trade,
    }

  def slot15_bot_status(self, asset: str, tab: dict[str, Any] | None = None) -> dict[str, Any]:
    asset = asset.lower()
    if asset == "eth" and not self._slot15m_enabled("eth"):
      return {"ok": False, "error": "ETH 15m disabled"}
    if tab is None:
      tab = self._slot15_tab(asset)
    slot_key = tab.get("slot_key") if tab.get("ok") else None
    status = self.slot15_bot_store(asset).status(slot_key)
    status["ok"] = True
    status["asset"] = asset
    status["slot_label"] = tab.get("slot_label") if tab else None
    store = self.slot15_bot_store(asset)
    status["recent_trades"] = store.list_trades(limit=100)
    status["slot_trades"] = (
      store.list_trades(limit=50, event_ticker=slot_key) if slot_key else []
    )
    open_pos = list(status.get("open_positions") or [])
    if tab and tab.get("ok"):
      from src.trading.slot15_bot import enrich_open_positions_live

      open_pos = enrich_open_positions_live(open_pos, tab)
      status["open_positions"] = open_pos
      unrealized = round(
        sum(float(p.get("unrealized_pnl_usd") or 0) for p in open_pos),
        2,
      )
      ss = status.get("slot_summary")
      if ss:
        ss = dict(ss)
        realized = float(ss.get("realized_pnl_usd") or 0)
        ss["unrealized_pnl_usd"] = unrealized
        ss["total_pnl_usd"] = round(realized + unrealized, 2)
        status["slot_summary"] = ss
    kalshi = self._kalshi_for(asset)
    status["kalshi_authenticated"] = bool(kalshi and kalshi.authenticated)
    if tab and tab.get("ok"):
      pred = tab.get("prediction") or {}
      monitor = tab.get("monitor") or {}
      status["entry_watch"] = {
        "signal": pred.get("signal"),
        "model_signal": pred.get("model_signal"),
        "prob_up": pred.get("prob_up"),
        "late_entry_action": monitor.get("late_entry_action") or "",
        "flip_action": monitor.get("flip_action") or "",
        "monitor_action": monitor.get("action"),
        "last_entry_attempt": self.slot15_bot_store(asset).last_entry_attempt(),
      }
    status["auto_tuning"] = store.get_auto_tuning()
    return status

  def _ensure_slot_prediction_current(self, asset: str) -> None:
    """Refresh in-memory prediction when the active slot rolled but cron has not run yet."""
    asset = asset.lower()
    if asset == "eth" and not self._slot15m_enabled("eth"):
      return
    slot_key = floor_to_15m(pd.Timestamp(datetime.now(timezone.utc)), self.tz)
    state = self._slot_state(asset)
    pred = state["latest_prediction"]
    if pred is not None and pred.slot_start is not None:
      if floor_to_15m(pred.slot_start, self.tz) == slot_key:
        return
    try:
      self._run_slot_prediction(asset)
    except Exception as e:
      log.warning("%s 15m: could not refresh slot prediction at rollover: %s", asset.upper(), e)

  def _run_slot15_bot_continuous(self, asset: str) -> None:
    asset = asset.lower()
    if asset == "eth" and not self._slot15m_enabled("eth"):
      return
    store = self.slot15_bot_store(asset)
    settings = store.get_settings()
    active = settings.enabled and settings.continuous
    try:
      if not active:
        if not settings.enabled:
          store.set_last_skip_reason("auto_bet_off")
        return
      self._ensure_slot_prediction_current(asset)
      tab = self._slot15_tab(asset)
      if tab.get("ok"):
        acfg = self._acfg_15m(asset)
        self.slot15_bot(asset).run_continuous_cycle(tab, cfg=acfg)
    except Exception as e:
      log.exception("%s 15m bot continuous cycle failed: %s", asset.upper(), e)
    finally:
      store.record_cycle(active=active)

  def run_slot15_bot_continuous(self) -> None:
    self._run_slot15_bot_continuous("btc")

  def run_eth_slot15_bot_continuous(self) -> None:
    if not self._slot15m_enabled("eth"):
      return
    self._run_slot15_bot_continuous("eth")

  def eth_hourly_prediction(self) -> dict[str, Any]:
    return self._hourly_tab_prediction("eth")

  def _hourly_tab_prediction(self, asset: str) -> dict[str, Any]:
    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))
    if not acfg.get("daily", {}).get("enabled", True):
      return {"ok": False, "error": f"{asset.upper()} hourly predictions disabled"}
    quote = self.live_price_quote(fresh=True, asset=asset)
    price = quote.price if quote else None
    storage = self.storage if asset == "btc" else self.eth_storage()
    if price is None:
      df_1m = storage.load("1m")
      if not df_1m.empty:
        price = float(df_1m["close"].iloc[-1])
    index_label = index_id_for_cfg(acfg)
    if price is None or price <= 0:
      return {"ok": False, "error": f"Live {index_label} unavailable"}
    df_1h = self._ohlc_1h(storage=storage)
    df_15m = storage.load("15m")
    predictor = self.hourly_predictor() if asset == "btc" else self.eth_hourly_predictor()
    tracker = self._asset_hourly_calibration(asset)
    cal_15m = self._calibration_for(asset) if asset == "eth" else self.calibration
    live = predictor.predict(
      current_price=float(price),
      df_1h=df_1h,
      df_15m=df_15m if not df_15m.empty else None,
      calibration_tracker=cal_15m,
    )
    if not live.get("ok"):
      return live

    from src.models.hourly_snapshot import (
      hour_open_prediction_from_row,
      late_call_prediction_from_row,
      locked_prediction_from_row,
    )

    event_ticker = (live.get("event") or {}).get("event_ticker")
    locked = None
    hour_open = None
    late_call = None
    if event_ticker:
      row = tracker.get_logged(event_ticker)
      if row:
        locked = locked_prediction_from_row(row, acfg)
        late_call = late_call_prediction_from_row(row, acfg)
        if late_call:
          from src.trading.hourly_position_alert import assess_late_call_position_alert_from_row

          late_call["position_alert"] = assess_late_call_position_alert_from_row(
            row, acfg, live_price=float(price) if price else None
          )
      open_row = tracker.get_hour_open(event_ticker)
      if open_row:
        hour_open = hour_open_prediction_from_row(open_row, acfg, index_label=index_label)

    out = {
      **live,
      "live": live,
      "locked": locked,
      "has_locked": locked is not None,
      "hour_open": hour_open,
      "has_hour_open": hour_open is not None,
      "late_call": late_call,
      "has_late_call": late_call is not None,
      "asset": asset,
    }
    if quote:
      out["brti_live"] = round(quote.price, 2)
      out["brti_source"] = quote.source
      live["brti_live"] = out["brti_live"]
      live["brti_source"] = quote.source
    out["timezone"] = self.tz
    live["timezone"] = self.tz
    live["asset"] = asset
    live["index_id"] = index_label
    pf = acfg.get("price_feed") or {}
    out["price_feed"] = pf.get("label", index_label)
    out["settlement_reference"] = pf.get("settlement_reference", index_label)

    if locked:
      mu_shift = None
      if locked.get("terminal_mu") is not None and live.get("terminal_mu") is not None:
        mu_shift = round(float(live["terminal_mu"]) - float(locked["terminal_mu"]), 2)
      out["live_vs_locked"] = {
        "mu_shift": mu_shift,
        "reference_at_log": locked.get("reference_price"),
        "logged_at": locked.get("logged_at"),
      }
      live["live_vs_locked"] = out["live_vs_locked"]

    if hour_open:
      mu_shift = None
      if hour_open.get("terminal_mu") is not None and live.get("terminal_mu") is not None:
        mu_shift = round(float(live["terminal_mu"]) - float(hour_open["terminal_mu"]), 2)
      out["live_vs_hour_open"] = {
        "mu_shift": mu_shift,
        "reference_at_log": hour_open.get("reference_price"),
        "logged_at": hour_open.get("logged_at"),
      }
      live["live_vs_hour_open"] = out["live_vs_hour_open"]

    if locked and hour_open:
      mu_shift = None
      if locked.get("terminal_mu") is not None and hour_open.get("terminal_mu") is not None:
        mu_shift = round(float(locked["terminal_mu"]) - float(hour_open["terminal_mu"]), 2)
      out["hour_open_vs_locked"] = {
        "mu_shift": mu_shift,
        "hour_open_at": hour_open.get("logged_at"),
        "locked_at": locked.get("logged_at"),
      }

    from src.trading.hourly_guidance import build_hourly_guidance
    from src.trading.hourly_intrahour_alert import assess_intrahour_opportunity

    out["guidance"] = build_hourly_guidance(
      live, locked, hour_open=hour_open, asset=asset, index_id=index_label, cfg=acfg
    )

    intrahour = assess_intrahour_opportunity(
      live=live,
      locked=locked,
      hour_open=hour_open,
      current_price=float(price) if price else None,
      index_label=index_label,
      cfg=acfg,
    )
    out["intrahour_opportunity"] = intrahour
    out["has_intrahour_opportunity"] = bool(intrahour and intrahour.get("highlight"))

    if asset == "btc":
      self.latest_hourly_prediction = out
      out["bot"] = self.hourly_bot_status("btc", out)
    else:
      self.latest_eth_hourly_prediction = out
      out["bot"] = self.hourly_bot_status("eth", out)
    return out

  def run_hourly_prediction(self, *, force: bool = False) -> dict[str, Any] | None:
    return self._run_hourly_prediction_for_asset("btc", force=force)

  def run_hourly_open_snapshot(self) -> dict[str, Any] | None:
    return self._run_hourly_open_for_asset("btc")

  def run_eth_hourly_open_snapshot(self) -> dict[str, Any] | None:
    if not asset_enabled(self.cfg, "eth"):
      return None
    acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
    if not acfg.get("hourly", {}).get("enabled", True):
      return None
    return self._run_hourly_open_for_asset("eth")

  def run_eth_hourly_prediction(self, *, force: bool = False) -> dict[str, Any] | None:
    if not asset_enabled(self.cfg, "eth"):
      return None
    acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
    if not acfg.get("hourly", {}).get("enabled", True):
      return None
    return self._run_hourly_prediction_for_asset("eth", force=force)

  def run_hourly_late_call(self, *, force: bool = False) -> dict[str, Any] | None:
    return self._run_hourly_late_call_for_asset("btc", force=force)

  def run_eth_hourly_late_call(self, *, force: bool = False) -> dict[str, Any] | None:
    if not asset_enabled(self.cfg, "eth"):
      return None
    acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
    if not acfg.get("hourly", {}).get("enabled", True):
      return None
    return self._run_hourly_late_call_for_asset("eth", force=force)

  def _run_hourly_late_call_for_asset(self, asset: str, *, force: bool = False) -> dict[str, Any] | None:
    """Log :45 ET late-call snapshot — trading guidance only, not calibration."""
    from datetime import datetime, timezone

    from src.models.hourly_late_call_log import prediction_to_late_call_row

    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))
    if not acfg.get("hourly", {}).get("enabled", True):
      return None
    try:
      out = self._hourly_tab_prediction(asset)
      if not out.get("ok"):
        return out
      live = out.get("live") or out
      event_ticker = (live.get("event") or {}).get("event_ticker")
      if not event_ticker:
        return out
      now = datetime.now(timezone.utc).isoformat()
      row = prediction_to_late_call_row(live, logged_at=now)
      row["asset"] = asset
      tracker = self._asset_hourly_calibration(asset)
      if tracker.log_late_call(row, force=force):
        log.info(
          "%s hourly late call logged: %s %s %s",
          asset.upper(),
          event_ticker,
          row.get("late_call_primary_signal"),
          row.get("late_call_primary_label"),
        )
      out = self._hourly_tab_prediction(asset)
      return out
    except Exception as e:
      log.exception("%s hourly late call failed: %s", asset.upper(), e)
      self.last_error = str(e)
      return None

  def _run_hourly_prediction_for_asset(self, asset: str, *, force: bool = False) -> dict[str, Any] | None:
    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))
    if not acfg.get("hourly", {}).get("enabled", True):
      return None
    try:
      self.resolve_hourly_outcomes(asset=asset)
      out = self._hourly_tab_prediction(asset)
      if not out.get("ok"):
        return out
      predictor = self.hourly_predictor() if asset == "btc" else self.eth_hourly_predictor()
      row = predictor.to_log_row(out.get("live") or out)
      tracker = self._asset_hourly_calibration(asset)
      if row.get("event_ticker"):
        tracker.log_prediction(row, force=force)
        log.info(
          "%s hourly prediction logged: %s %s %s",
          asset.upper(),
          row["event_ticker"],
          row.get("primary_signal"),
          row.get("primary_label"),
        )
        out = self._hourly_tab_prediction(asset)
      return out
    except Exception as e:
      log.exception("%s hourly prediction failed: %s", asset.upper(), e)
      self.last_error = str(e)
      return None

  def _run_hourly_open_for_asset(self, asset: str) -> dict[str, Any] | None:
    """Log hour-open snapshot at :00 ET — preview only, not used for calibration."""
    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))
    if not acfg.get("hourly", {}).get("enabled", True):
      return None
    if not acfg.get("hourly", {}).get("hour_open_snapshot", True):
      return None
    try:
      preview = self._hourly_tab_prediction(asset)
      if not preview.get("ok"):
        return preview
      predictor = self.hourly_predictor() if asset == "btc" else self.eth_hourly_predictor()
      row = predictor.to_log_row(preview.get("live") or preview)
      tracker = self._asset_hourly_calibration(asset)
      if row.get("event_ticker"):
        tracker.log_open_snapshot(row)
        log.info(
          "%s hourly hour-open snapshot: %s %s %s",
          asset.upper(),
          row["event_ticker"],
          row.get("primary_signal"),
          row.get("primary_label"),
        )
      return self._hourly_tab_prediction(asset)
    except Exception as e:
      log.exception("%s hourly hour-open snapshot failed: %s", asset.upper(), e)
      self.last_error = str(e)
      return None

  def resolve_hourly_outcomes(self, *, asset: str = "btc") -> None:
    from src.data.kalshi_hourly import try_resolve_pending

    tracker = self._asset_hourly_calibration(asset)
    pending = tracker.get_pending()
    if not pending:
      return
    resolved = 0
    for row in pending:
      res = try_resolve_pending(self.kalshi, row)
      if res is None:
        continue
      if tracker.resolve(str(row["event_ticker"]), res):
        resolved += 1
    if resolved:
      log.info("Resolved %d %s hourly predictions via Kalshi", resolved, asset.upper())
      if asset == "btc":
        self.refit_hourly_calibrator()
        self.calibrate_hourly_sigma()
      else:
        self.refit_eth_hourly_calibrator()
        self.calibrate_eth_hourly_sigma()

  def resolve_eth_hourly_outcomes(self) -> None:
    self.resolve_hourly_outcomes(asset="eth")

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
    self._calibrate_hourly_sigma_for_asset("btc")

  def calibrate_eth_hourly_sigma(self) -> None:
    self._calibrate_hourly_sigma_for_asset("eth")

  def _calibrate_hourly_sigma_for_asset(self, asset: str) -> None:
    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))
    if not acfg.get("hourly", {}).get("sigma_calibration", True):
      return
    tracker = self._asset_hourly_calibration(asset)
    df = tracker.load_resolved()
    if len(df) < 10:
      return
    err = (pd.to_numeric(df["settle_brti"], errors="coerce") - pd.to_numeric(df["blended_mu"], errors="coerce")).abs()
    sigma = pd.to_numeric(df["terminal_sigma"], errors="coerce").replace(0, np.nan)
    ratio = float((err / sigma).median())
    if ratio > 0 and not np.isnan(ratio):
      hp = self.hourly_predictor() if asset == "btc" else self.eth_hourly_predictor()
      new_scale = max(0.5, min(2.0, hp._sigma_scale * ratio))
      hp.save_sigma_scale(new_scale)
      log.info("%s hourly sigma scale updated to %.3f", asset.upper(), new_scale)

  def refit_eth_hourly_calibrator(self) -> bool:
    if not asset_enabled(self.cfg, "eth"):
      return False
    hp = self.eth_hourly_predictor()
    if hp.calibrator is None:
      return False
    if self.eth_hourly_calibration and self.eth_hourly_calibration.fit_calibrator(hp.calibrator):
      from src.models.hourly_trainer import HourlyModelTrainer

      acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
      trainer = HourlyModelTrainer(acfg)
      trainer.model = hp.model
      trainer.feature_names = hp.feature_names
      trainer.calibrator = hp.calibrator
      path = Path(acfg["paths"]["models"]) / "model_hourly.joblib"
      if path.exists() and hp.model is not None:
        trainer.save(path)
      log.info("ETH hourly calibrator refit from resolved events")
      return True
    return False

  def train_hourly_model(self, min_samples: int | None = None, *, asset: str = "btc") -> None:
    from src.models.hourly_trainer import HourlyModelTrainer

    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, "eth"))
    storage = self.storage if asset == "btc" else self.eth_storage()
    status_attr = "hourly_train_status" if asset == "btc" else "eth_hourly_train_status"
    setattr(self, status_attr, {
      "state": "running",
      "started_at": datetime.now(timezone.utc).isoformat(),
      "asset": asset,
    })
    try:
      cfg = acfg
      if min_samples is not None:
        cfg = {**acfg, "hourly": {**acfg.get("hourly", {}), "min_train_samples": min_samples}}
      df_1h = self._ohlc_1h(storage=storage)
      df_15m = storage.load("15m")
      if df_1h.empty:
        raise ValueError(f"No 1h candle data for {asset.upper()} — enable 1h fetch in config")
      trainer = HourlyModelTrainer(cfg)
      metrics = trainer.train(df_1h, df_15m if not df_15m.empty else None)
      model_path = Path(cfg["paths"]["models"]) / "model_hourly.joblib"
      trainer.save(model_path)
      if asset == "btc":
        self._hourly_predictor = None
      else:
        self._eth_hourly_predictor = None
      setattr(self, status_attr, {
        "state": "done",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_path": str(model_path),
        "metrics": metrics,
        "candles_1h": len(df_1h),
        "asset": asset,
      })
      log.info("%s hourly model training complete: %s", asset.upper(), metrics)
    except Exception as e:
      log.exception("%s hourly model training failed", asset.upper())
      setattr(self, status_attr, {
        "state": "error",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": str(e),
        "asset": asset,
      })

  def train_eth_hourly_model(self, min_samples: int | None = None) -> None:
    self.train_hourly_model(min_samples, asset="eth")


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

    if asset_enabled(self.cfg, "eth"):
      self._fetch_and_store_eth()

  def _fetch_and_store_eth(self) -> None:
    acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
    fetcher = self.eth_fetcher()
    storage = self.eth_storage()
    try:
      df_1m = fetcher.fetch_latest_candles("1m", count=240)
      if not df_1m.empty:
        storage.save("1m", df_1m)
    except Exception as e:
      log.warning("Failed to fetch ETH 1m: %s", e)
    try:
      count_1h = int(acfg.get("hourly", {}).get("fetch_candles_1h", 720))
      df_1h = fetcher.fetch_latest_candles("1h", count=count_1h)
      if not df_1h.empty:
        storage.save("1h", df_1h)
    except Exception as e:
      log.warning("Failed to fetch ETH 1h: %s", e)
    if acfg.get("kalshi", {}).get("enabled", True):
      try:
        df_15m = fetcher.fetch_latest_candles("15m", count=self.fetch_15m_count)
        if not df_15m.empty:
          storage.save("15m", df_15m)
      except Exception as e:
        log.warning("Failed to fetch ETH 15m: %s", e)

  def resolve_outcomes(self) -> None:
    self.resolve_hourly_outcomes(asset="btc")
    if asset_enabled(self.cfg, "eth"):
      self.resolve_eth_hourly_outcomes()
    self._resolve_slot_outcomes("btc")
    if self._slot15m_enabled("eth"):
      self._resolve_slot_outcomes("eth")

  def _resolve_slot_outcomes(self, asset: str) -> None:
    kalshi = self._kalshi_for(asset)
    calibration = self._calibration_for(asset)
    pending = calibration.get_pending()
    if not pending:
      return

    index_id = index_id_for_cfg(self._acfg_15m(asset))
    price_lookup: dict[str, PredictionResolution] = {}
    for _row_id, ts_str, entry_price in pending:
      slot_s = floor_to_15m(pd.Timestamp(ts_str), self.tz)
      settlement = kalshi.slot_settlement(slot_s)
      if settlement is None or not settlement.settled:
        continue

      resolution = kalshi.resolution_for_entry(float(entry_price), settlement)
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
      resolved = calibration.resolve_with_prices(price_lookup)
      log.info("Resolved %d %s 15m predictions via Kalshi %s", resolved, asset.upper(), index_id)
      if asset == "btc":
        self._log_postmortems(price_lookup.keys())
      else:
        self.refit_eth_calibrator()
        self.refit_eth_second_chance_calibrator()

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
      self.refit_second_chance_calibrator()
    except Exception as e:
      log.warning("Postmortem logging failed: %s", e)

  def refit_eth_calibrator(self) -> bool:
    predictor = self._predictor_for("eth")
    if predictor.model is None:
      return False
    from src.models.trainer import ModelTrainer

    acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
    trainer = ModelTrainer(acfg)
    trainer.model = predictor.model
    trainer.feature_names = predictor.feature_names
    if trainer.fit_calibrator_from_tracker(self.eth_calibration):
      predictor.calibrator = trainer.calibrator
      model_path = Path(acfg["paths"]["models"]) / "model.joblib"
      if model_path.exists():
        trainer.save(model_path)
      self._eth_predictor = None
      log.info(
        "ETH 15m calibrator refit from %d resolved slots",
        len(self.eth_calibration.load_resolved()) if self.eth_calibration else 0,
      )
      return True
    return False

  def refit_eth_second_chance_calibrator(self) -> bool:
    if not asset_enabled(self.cfg, "eth"):
      return False
    acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
    scfg = acfg.get("second_chance", {})
    if not scfg.get("enabled", True) or not scfg.get("calibrate", True):
      return False
    advisor = self.eth_second_chance_advisor()
    if advisor.model is None or self.eth_calibration is None:
      return False
    if self.eth_calibration.fit_second_chance_calibrator(advisor.calibrator):
      from src.models.second_chance_trainer import SecondChanceTrainer

      trainer = SecondChanceTrainer(acfg)
      trainer.model = advisor.model
      trainer.feature_names = advisor.feature_names
      trainer.calibrator = advisor.calibrator
      path = Path(acfg["paths"]["models"]) / "model_second_chance.joblib"
      if path.exists():
        trainer.save(path)
      self._eth_second_chance_advisor = None
      log.info("ETH 2nd Chance calibrator refit from resolved slots")
      return True
    return False

  def refit_second_chance_calibrator(self) -> bool:
    scfg = self.cfg.get("second_chance", {})
    if not scfg.get("enabled", True) or not scfg.get("calibrate", True):
      return False
    advisor = self.second_chance_advisor()
    if advisor.model is None:
      return False
    if self.calibration.fit_second_chance_calibrator(advisor.calibrator):
      from src.models.second_chance_trainer import SecondChanceTrainer
      trainer = SecondChanceTrainer(self.cfg)
      trainer.model = advisor.model
      trainer.feature_names = advisor.feature_names
      trainer.calibrator = advisor.calibrator
      path = Path(self.cfg["paths"]["models"]) / "model_second_chance.joblib"
      if path.exists():
        trainer.save(path)
      log.info("2nd Chance calibrator refit from resolved slots")
      return True
    return False

  def train_second_chance_model(self, min_samples: int | None = None, *, asset: str = "btc") -> None:
    from src.models.second_chance_trainer import SecondChanceTrainer

    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, "eth"))
    storage = self.storage if asset == "btc" else self.eth_storage()
    predictor = self.predictor if asset == "btc" else self._predictor_for("eth")
    status_attr = "second_chance_train_status" if asset == "btc" else "eth_second_chance_train_status"
    setattr(self, status_attr, {
      "state": "running",
      "started_at": datetime.now(timezone.utc).isoformat(),
      "asset": asset,
    })
    try:
      cfg = acfg
      if min_samples is not None:
        cfg = {**acfg, "second_chance": {**acfg.get("second_chance", {}), "min_train_samples": min_samples}}
      df_1m = storage.load("1m")
      df_15m = storage.load("15m")
      if df_1m.empty:
        raise ValueError(f"No 1m candle data for {asset.upper()} 2nd Chance training")
      trainer = SecondChanceTrainer(cfg)
      metrics = trainer.train(
        df_1m,
        df_15m if not df_15m.empty else None,
        main_model=predictor.model,
        main_feature_names=predictor.feature_names,
        main_calibrator=predictor.calibrator,
      )
      model_path = Path(cfg["paths"]["models"]) / "model_second_chance.joblib"
      trainer.save(model_path)
      if asset == "btc":
        self._second_chance_advisor = None
      else:
        self._eth_second_chance_advisor = None
      setattr(self, status_attr, {
        "state": "done",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_path": str(model_path),
        "metrics": metrics,
        "candles_1m": len(df_1m),
        "asset": asset,
      })
      log.info("%s 2nd Chance model trained: %s", asset.upper(), metrics)
    except Exception as e:
      log.exception("%s 2nd Chance model training failed", asset.upper())
      setattr(self, status_attr, {
        "state": "error",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": str(e),
        "asset": asset,
      })

  def train_eth_second_chance_model(self, min_samples: int | None = None) -> None:
    self.train_second_chance_model(min_samples, asset="eth")

  def run_second_chance(self) -> dict[str, Any] | None:
    out = self._run_second_chance_for_asset("btc")
    if self._slot15m_enabled("eth"):
      self._run_second_chance_for_asset("eth")
    return out

  def _run_second_chance_for_asset(self, asset: str) -> dict[str, Any] | None:
    """Log 2nd Chance reassessment at t+4min for the active slot."""
    acfg = self._acfg_15m(asset)
    scfg = acfg.get("second_chance", {})
    if not scfg.get("enabled", True):
      return None
    state = self._slot_state(asset)
    kalshi = self._kalshi_for(asset)
    storage = self.storage if asset == "btc" else self.eth_storage()
    calibration = self._calibration_for(asset)
    advisor = self.second_chance_advisor() if asset == "btc" else self.eth_second_chance_advisor()
    try:
      self.fetch_and_store()
      now = pd.Timestamp(datetime.now(timezone.utc))
      slot_s = current_slot_start(now, self.tz)
      key = self._slot_cache_key(slot_s)
      if key in state["second_chance_logged"]:
        return None

      pred = self._prediction_for_current_slot(asset=asset)
      if pred is None:
        log.warning("%s 2nd Chance skipped — no opening prediction for slot %s", asset.upper(), slot_s)
        return None

      slot_e = slot_end(slot_s, self.tz)
      seconds_remaining = max(0, int((slot_e - now).total_seconds()))
      elapsed_min = (now - slot_s).total_seconds() / 60.0
      min_elapsed = float(scfg.get("elapsed_minutes", 4))
      if elapsed_min < min_elapsed - 0.25:
        log.debug("%s 2nd Chance skipped — %.1f min elapsed (need %.0f)", asset.upper(), elapsed_min, min_elapsed)
        return None

      api_ref, _ = kalshi.slot_t0_reference(slot_s, fresh=True)
      ref = float(api_ref) if api_ref else float(pred.get("reference_price") or pred.get("price") or 0)
      if ref <= 0:
        locked = self._locked_slot_reference(slot_s, asset=asset)
        if locked:
          ref = float(locked["price"])
      if ref <= 0:
        log.warning("%s 2nd Chance skipped — no reference for slot %s", asset.upper(), slot_s)
        return None

      live_quote = self.live_price_quote(fresh=True, asset=asset)
      current = live_quote.price if live_quote else ref
      df_1m = storage.load("1m")

      decision = advisor.evaluate(
        open_prob_up=float(pred.get("prob_up", 0.5)),
        open_signal=str(pred.get("signal", "NO TRADE")),
        reference_price=ref,
        current_price=current,
        df_1m=df_1m if not df_1m.empty else None,
        slot_start=slot_s,
        seconds_remaining=seconds_remaining,
      )

      ts = floor_to_15m(slot_s, self.tz).isoformat()
      if calibration.record_second_chance(
        ts,
        decision.signal,
        decision.prob_up,
        seconds_remaining,
        confidence=decision.confidence,
        expected_move=decision.expected_move_pct,
      ):
        state["second_chance_logged"].add(key)
        log.info(
          "%s 2nd Chance logged: %s %s %.0f%% UP (%ds left, method=%s)",
          asset.upper(),
          ts,
          decision.signal,
          decision.prob_up * 100,
          seconds_remaining,
          decision.method,
        )
        return {
          "slot": ts,
          "signal": decision.signal,
          "prob_up": decision.prob_up,
          "confidence": decision.confidence,
          "summary": decision.summary,
          "method": decision.method,
          "asset": asset,
        }
      return None
    except Exception as e:
      log.exception("%s 2nd Chance failed: %s", asset.upper(), e)
      if asset == "btc":
        self.last_error = str(e)
      else:
        self.eth_last_error = str(e)
      return None

  def collect_auxiliary(self) -> None:
    try:
      collector = HistoricalCollector(self.cfg)
      counts = collector.collect_auxiliary()
      log.info("Auxiliary data refreshed: %s", counts)
    except Exception as e:
      log.warning("Auxiliary collect failed: %s", e)

  def train_model(self, min_samples: int | None = None, *, asset: str = "btc") -> None:
    """Train 15m LightGBM in-process (intended for background thread)."""
    from src.models.trainer import ModelTrainer

    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, "eth"))
    storage = self.storage if asset == "btc" else self.eth_storage()
    status_attr = "train_status" if asset == "btc" else "eth_train_status"
    setattr(self, status_attr, {
      "state": "running",
      "started_at": datetime.now(timezone.utc).isoformat(),
      "asset": asset,
    })
    try:
      cfg = acfg
      if min_samples is not None:
        cfg = {**acfg, "model": {**acfg.get("model", {}), "min_train_samples": min_samples}}

      df_15m = storage.load("15m")
      df_1m = storage.load("1m")
      if df_15m.empty:
        raise ValueError(f"No 15m candle data for {asset.upper()} — run collect first")

      trainer = ModelTrainer(cfg)
      metrics = trainer.train(df_15m, df_1m if not df_1m.empty else None)
      model_path = Path(cfg["paths"]["models"]) / "model.joblib"
      trainer.save(model_path)
      if asset == "btc":
        self.predictor.load_model(str(model_path))
      else:
        self._eth_predictor = None
      setattr(self, status_attr, {
        "state": "done",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_path": str(model_path),
        "metrics": metrics,
        "candles_15m": len(df_15m),
        "candles_1m": len(df_1m),
        "asset": asset,
      })
      log.info("%s 15m model training complete: %s", asset.upper(), metrics)
    except Exception as e:
      log.exception("%s 15m model training failed", asset.upper())
      setattr(self, status_attr, {
        "state": "error",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": str(e),
        "asset": asset,
      })

  def train_eth_model(self, min_samples: int | None = None) -> None:
    self.train_model(min_samples, asset="eth")

  def auto_retrain(self) -> None:
    """Daily scheduled retrain — runs in background."""
    acfg = self.cfg.get("auto_train", {})
    if not acfg.get("enabled", True):
      return
    if self.train_status.get("state") == "running":
      log.warning("Auto-retrain skipped: training already in progress")
      return
    log.info("Daily auto-retrain starting")
    threading.Thread(target=self.train_model, kwargs={"asset": "btc"}, daemon=True).start()
    if self.cfg.get("hourly", {}).get("enabled", True):
      threading.Thread(target=self.train_hourly_model, kwargs={"asset": "btc"}, daemon=True).start()
    if self.cfg.get("second_chance", {}).get("enabled", True):
      threading.Thread(target=self.train_second_chance_model, kwargs={"asset": "btc"}, daemon=True).start()
    if asset_enabled(self.cfg, "eth"):
      threading.Thread(target=self.train_eth_model, daemon=True).start()
      if (self._eth_cfg or {}).get("hourly", {}).get("enabled", True):
        threading.Thread(target=self.train_eth_hourly_model, daemon=True).start()
      if (self._eth_cfg or {}).get("second_chance", self.cfg.get("second_chance", {})).get("enabled", True):
        threading.Thread(target=self.train_eth_second_chance_model, daemon=True).start()

  def run_bot_auto_tuning(self) -> dict[str, Any]:
    """Tune bot entry thresholds from paper trade logs when enough history exists."""
    from src.trading.bot_auto_tuning import auto_tune_cfg, run_auto_tune_for_store

    tune_cfg = auto_tune_cfg(self.cfg)
    if not tune_cfg["enabled"]:
      return {"ok": False, "reason": "auto_tune_disabled"}

    results: list[dict[str, Any]] = []
    specs = (
      ("hourly", "btc", self.hourly_bot_store("btc"), self.cfg),
      ("hourly", "eth", self.hourly_bot_store("eth"), self._eth_cfg or self.cfg),
      ("slot15", "btc", self.slot15_bot_store("btc"), self._acfg_15m("btc")),
      ("slot15", "eth", self.slot15_bot_store("eth"), self._acfg_15m("eth")),
    )
    for kind, asset, store, acfg in specs:
      if asset == "eth" and kind == "slot15" and not self._slot15m_enabled("eth"):
        continue
      try:
        out = run_auto_tune_for_store(store, cfg=acfg, kind=kind)
        out["kind"] = kind
        out["asset"] = asset
        results.append(out)
        if out.get("reason") == "tuned":
          log.info(
            "%s %s bot auto-tuned: ask-edge=%s¢ kelly=%s — %s",
            asset.upper(),
            kind,
            out.get("min_ask_edge_cents"),
            out.get("kelly_fraction"),
            out.get("message"),
          )
      except Exception as e:
        log.warning("%s %s bot auto-tune failed: %s", asset.upper(), kind, e)
        results.append({"ok": False, "kind": kind, "asset": asset, "error": str(e)})

    return {"ok": True, "bots": results, "tuned_at": datetime.now(timezone.utc).isoformat()}

  def _schedule_hourly(self, scheduler) -> None:
    if self.cfg.get("hourly", {}).get("enabled", True):
      hcfg = self.cfg.get("hourly", {})
      if hcfg.get("hour_open_snapshot", True):
        open_minute = int(hcfg.get("open_log_minute", 0))
        scheduler.add_job(
          self.run_hourly_open_snapshot,
          CronTrigger(minute=str(open_minute), timezone=self.tz),
          id="hourly_open",
          max_instances=1,
        )
      minute = int(hcfg.get("log_minute", 5))
      scheduler.add_job(
        self.run_hourly_prediction,
        CronTrigger(minute=str(minute), timezone=self.tz),
        id="hourly_predict",
        max_instances=1,
      )
      late_minute = int(hcfg.get("late_call_minute", 45))
      scheduler.add_job(
        self.run_hourly_late_call,
        CronTrigger(minute=str(late_minute), timezone=self.tz),
        id="hourly_late_call",
        max_instances=1,
      )
      scheduler.add_job(
        self.refit_hourly_calibrator,
        "interval",
        hours=6,
        id="refit_hourly_calibrator",
        max_instances=1,
      )
      bot_cfg = hcfg.get("bot") or {}
      if bot_cfg.get("continuous_enabled", True):
        poll_sec = int(bot_cfg.get("poll_seconds", 5))
        scheduler.add_job(
          self.run_hourly_bot_continuous,
          "interval",
          seconds=poll_sec,
          id="hourly_bot_continuous",
          max_instances=1,
        )
    if asset_enabled(self.cfg, "eth"):
      acfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
      if acfg.get("hourly", {}).get("enabled", True):
        ehcfg = acfg.get("hourly", {})
        if ehcfg.get("hour_open_snapshot", True):
          open_minute = int(ehcfg.get("open_log_minute", 0))
          scheduler.add_job(
            self.run_eth_hourly_open_snapshot,
            CronTrigger(minute=str(open_minute), timezone=self.tz),
            id="eth_hourly_open",
            max_instances=1,
          )
        minute = int(ehcfg.get("log_minute", 5))
        scheduler.add_job(
          self.run_eth_hourly_prediction,
          CronTrigger(minute=str(minute), timezone=self.tz),
          id="eth_hourly_predict",
          max_instances=1,
        )
        late_minute = int(ehcfg.get("late_call_minute", 45))
        scheduler.add_job(
          self.run_eth_hourly_late_call,
          CronTrigger(minute=str(late_minute), timezone=self.tz),
          id="eth_hourly_late_call",
          max_instances=1,
        )
        bot_cfg = ehcfg.get("bot") or {}
        if bot_cfg.get("continuous_enabled", True):
          poll_sec = int(bot_cfg.get("poll_seconds", 5))
          scheduler.add_job(
            self.run_eth_hourly_bot_continuous,
            "interval",
            seconds=poll_sec,
            id="eth_hourly_bot_continuous",
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
    self._second_chance_logged.clear()
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
    return self._run_slot_prediction("btc")

  def run_eth_prediction(self) -> Prediction | None:
    if not self._slot15m_enabled("eth"):
      return None
    return self._run_slot_prediction("eth")

  def _run_slot_prediction(self, asset: str) -> Prediction | None:
    acfg = self._acfg_15m(asset)
    kalshi = self._kalshi_for(asset)
    storage = self.storage if asset == "btc" else self.eth_storage()
    predictor = self._predictor_for(asset)
    logger = self._logger_for(asset)
    state = self._slot_state(asset)
    err_attr = state["last_error_attr"]
    try:
      self.resolve_outcomes()
      self.fetch_and_store()

      df_15m = storage.load("15m")
      df_1m = storage.load("1m")

      if df_15m.empty or len(df_15m) < self.min_candles:
        self.fetch_and_store()
        df_15m = storage.load("15m")
        df_1m = storage.load("1m")

      if df_15m.empty or len(df_15m) < self.min_candles:
        msg = f"Need {self.min_candles}+ fifteen-minute candles, have {len(df_15m)}"
        setattr(self, err_attr, msg)
        log.error("%s 15m: %s", asset.upper(), msg)
        return None

      slot_s = floor_to_15m(pd.Timestamp(datetime.now(timezone.utc)), self.tz)
      kalshi_ref = self._resolve_kalshi_t0(slot_s, asset=asset)
      open_quote = self.live_price_quote(fresh=True, asset=asset)
      current_quote = self.live_price_quote(fresh=True, asset=asset)
      locked = self._locked_slot_reference(slot_s, asset=asset)
      locked_ref = kalshi_ref
      if locked_ref is None and locked:
        locked_ref = float(locked["price"])

      pred = predictor.predict(
        df_15m,
        df_1m if not df_1m.empty else None,
        live_price=kalshi_ref,
        current_price=current_quote.price if current_quote else None,
        locked_reference=locked_ref,
        live_trade_time=open_quote.trade_time if open_quote else None,
        current_trade_time=current_quote.trade_time if current_quote else None,
        kalshi_reference=kalshi_ref,
      )
      self._remember_slot_reference(pred, open_quote, kalshi_ref, asset=asset)
      active = kalshi.active_slot15m_market()
      kalshi_ticker = active.market_ticker if active else ""
      logger.log(pred, kalshi_market_ticker=kalshi_ticker)
      if asset == "btc":
        self.latest_prediction = pred
      else:
        self.latest_eth_prediction = pred
      setattr(self, err_attr, None)
      log.info(
        "%s slot %s: UP=%.1f%% signal=%s price=$%.2f",
        asset.upper(), pred.slot_label, pred.prob_up * 100, pred.signal.value, pred.price,
      )
      return pred
    except Exception as e:
      setattr(self, err_attr, str(e))
      log.exception("%s 15m prediction failed: %s", asset.upper(), e)
      return None

  def _resolve_kalshi_t0(self, slot_s: pd.Timestamp, *, asset: str = "btc", retries: int = 6, delay_sec: float = 0.5) -> float | None:
    """Kalshi floor_strike at slot open — retry briefly while market row populates."""
    kalshi = self._kalshi_for(asset)
    for attempt in range(retries):
      ref, _ = kalshi.slot_t0_reference(slot_s, fresh=True)
      if ref is not None and ref > 0:
        return float(ref)
      if attempt < retries - 1:
        time.sleep(delay_sec)
    log.warning("Kalshi floor_strike unavailable for %s slot %s after %d tries", asset.upper(), slot_s, retries)
    return None

  def _live_cache_sec(self) -> float:
    return float(self.cfg.get("kalshi", {}).get("brti_cache_sec", 0))

  def _live_fallback_enabled(self) -> bool:
    return bool(self.cfg.get("kalshi", {}).get("live_fallback_exchange", True))

  def _exchange_tick_cache_sec(self) -> float:
    return float(self.cfg.get("kalshi", {}).get("exchange_tick_cache_sec", 1.0))

  def _exchange_live_quote(self, *, fresh: bool = True, asset: str = "btc") -> KalshiPriceQuote | None:
    """Fresh exchange last trade — used when index auth is missing or stale."""
    cache = self._ticker_cache if asset == "btc" else self._eth_ticker_cache
    now_mono = time.monotonic()
    cache_sec = self._exchange_tick_cache_sec()
    if not fresh and cache and (now_mono - cache[1]) < cache_sec:
      return cache[0]
    try:
      fetcher = self.fetcher if asset == "btc" else self.eth_fetcher()
      ticker = fetcher.fetch_ticker_quote()
      trade_time = ticker.trade_time
      if trade_time is None:
        trade_time = datetime.now(timezone.utc)
      elif trade_time.tzinfo is None:
        trade_time = trade_time.replace(tzinfo=timezone.utc)
      source = "exchange_live" if not self.kalshi.authenticated else "exchange_fallback"
      quote = KalshiPriceQuote(price=ticker.price, source=source, trade_time=trade_time)
      if asset == "btc":
        self._ticker_cache = (quote, now_mono)
      else:
        self._eth_ticker_cache = (quote, now_mono)
      return quote
    except Exception as e:
      log.warning("%s exchange live tick failed: %s", asset.upper(), e)
      if cache:
        return cache[0]
      return None

  def _live_quote(self, *, fresh: bool = False) -> KalshiPriceQuote | None:
    """Kalshi BRTI live price for P&L and display."""
    return self.kalshi.live_quote(fresh=fresh)

  def live_price_quote(self, *, fresh: bool = True, asset: str = "btc") -> KalshiPriceQuote | None:
    """CF Benchmarks index when authed; otherwise fresh exchange tick."""
    acfg = self.cfg if asset == "btc" else (self._eth_cfg or asset_cfg(self.cfg, asset))
    index_id = index_id_for_cfg(acfg)
    max_stale = float(self.cfg.get("kalshi", {}).get("brti_max_stale_sec", 5))
    if self.kalshi.authenticated:
      live = self.kalshi.fetch_index_live(index_id, fresh=True)
      if live is not None:
        return live
      last = self.kalshi.last_index_quote(index_id)
      if last is not None and last.age_sec is not None and last.age_sec <= max_stale:
        return last
    if self._live_fallback_enabled():
      return self._exchange_live_quote(fresh=fresh, asset=asset)
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
    *,
    asset: str = "btc",
  ) -> None:
    if pred.slot_start is None:
      return
    key = self._slot_cache_key(pred.slot_start)
    cache = self._slot_state(asset)["slot_tick_cache"]
    cache[key] = {
      "price": pred.reference_price,
      "source": pred.reference_source,
      "trade_time": pred.reference_trade_time or (quote.trade_time.isoformat() if quote and quote.trade_time else None),
    }
    if kalshi_ref:
      self._kalshi_for(asset)._slot_targets[key] = float(kalshi_ref)

  def _locked_slot_reference(self, slot_s: pd.Timestamp, *, asset: str = "btc") -> dict[str, Any] | None:
    cache = self._slot_state(asset)["slot_tick_cache"]
    return cache.get(self._slot_cache_key(slot_s))

  def _prediction_for_current_slot(self, *, asset: str = "btc") -> dict[str, Any] | None:
    """DB/logged prediction for the slot that is active right now."""
    state = self._slot_state(asset)
    calibration = self._calibration_for(asset)
    slot_s = current_slot_start(tz_name=self.tz)
    slot_key = floor_to_15m(slot_s, self.tz)

    latest = state["latest_prediction"]
    if latest and latest.slot_start is not None:
      if floor_to_15m(latest.slot_start, self.tz) == slot_key:
        p = latest
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
      df = calibration.load_recent(12)
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
      log.warning("Could not load %s slot prediction: %s", asset.upper(), e)
      return None

  def slot_monitor(self, reference_override: float | None = None) -> SlotMonitor:
    return self._slot_monitor_for_asset("btc", reference_override)

  def eth_slot_monitor(self, reference_override: float | None = None) -> SlotMonitor:
    return self._slot_monitor_for_asset("eth", reference_override)

  def _slot_monitor_for_asset(self, asset: str, reference_override: float | None = None) -> SlotMonitor:
    """Live hold / take-profit / cut-loss guidance for the active 15m window."""
    kalshi = self._kalshi_for(asset)
    storage = self.storage if asset == "btc" else self.eth_storage()
    calibration = self._calibration_for(asset)
    state = self._slot_state(asset)
    now = pd.Timestamp(datetime.now(timezone.utc))
    slot_s = current_slot_start(now, self.tz)
    df_1m = storage.load("1m")

    pred = self._prediction_for_current_slot(asset=asset)
    live_quote = self.live_price_quote(fresh=True, asset=asset)
    current = live_quote.price if live_quote else None

    api_ref, ref_source = kalshi.slot_t0_reference(slot_s, fresh=True)
    if api_ref is None and pred is not None:
      api_ref = float(pred.get("reference_price") or pred.get("price") or 0)
      ref_source = str(pred.get("reference_source") or kalshi._index_target_source())
    elif api_ref is None:
      locked = self._locked_slot_reference(slot_s, asset=asset)
      if locked:
        api_ref = float(locked["price"])
        ref_source = str(locked.get("source") or kalshi._index_target_source())

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
    monitor.kalshi = kalshi.active_market_summary()
    self._attach_second_chance_preview(slot_s, pred, monitor, asset=asset, calibration=calibration)
    self._maybe_log_late_entry(slot_s, monitor, asset=asset, calibration=calibration, state=state)
    self._maybe_log_flip(slot_s, monitor, asset=asset, calibration=calibration, state=state)
    return monitor

  def _maybe_log_flip(
    self,
    slot_s: pd.Timestamp,
    monitor,
    *,
    asset: str = "btc",
    calibration: CalibrationTracker | None = None,
    state: dict[str, Any] | None = None,
  ) -> None:
    action = monitor.action.value if hasattr(monitor.action, "value") else str(monitor.action)
    if action not in ("FLIP LONG", "FLIP SHORT"):
      return
    state = state or self._slot_state(asset)
    key = self._slot_cache_key(slot_s)
    if key in state["flip_logged"]:
      return
    prob = monitor.reassessed_prob_up
    if prob is None:
      return
    ts = floor_to_15m(slot_s, self.tz).isoformat()
    tracker = calibration or self._calibration_for(asset)
    if tracker.record_flip(ts, action, float(prob), int(monitor.seconds_remaining)):
      state["flip_logged"].add(key)
      log.info("%s flip logged: %s %.0f%% UP (%ds left)", asset.upper(), action, prob * 100, monitor.seconds_remaining)

  def _maybe_log_late_entry(
    self,
    slot_s: pd.Timestamp,
    monitor,
    *,
    asset: str = "btc",
    calibration: CalibrationTracker | None = None,
    state: dict[str, Any] | None = None,
  ) -> None:
    action = getattr(monitor, "late_entry_action", "") or ""
    if action not in ("LATE LONG", "LATE SHORT"):
      return
    state = state or self._slot_state(asset)
    key = self._slot_cache_key(slot_s)
    if key in state["late_entry_logged"]:
      return
    prob = monitor.reassessed_prob_up
    if prob is None:
      return
    ts = floor_to_15m(slot_s, self.tz).isoformat()
    tracker = calibration or self._calibration_for(asset)
    if tracker.record_late_entry(ts, action, float(prob), int(monitor.seconds_remaining)):
      state["late_entry_logged"].add(key)
      log.info("%s late entry logged: %s %.0f%% UP (%ds left)", asset.upper(), action, prob * 100, monitor.seconds_remaining)

  def _attach_second_chance_preview(
    self,
    slot_s: pd.Timestamp,
    pred: dict[str, Any] | None,
    monitor: SlotMonitor,
    *,
    asset: str = "btc",
    calibration: CalibrationTracker | None = None,
  ) -> None:
    """Show logged 2nd Chance on slot monitor when available."""
    if pred is None:
      return
    try:
      tracker = calibration or self._calibration_for(asset)
      df = tracker.load_recent(8)
      if df.empty or "second_chance_signal" not in df.columns:
        return
      slot_key = floor_to_15m(slot_s, self.tz)
      df = df.copy()
      df["_slot"] = pd.to_datetime(df["timestamp"], utc=True).apply(
        lambda t: floor_to_15m(t, self.tz)
      )
      match = df[df["_slot"] == slot_key]
      if match.empty:
        return
      row = match.iloc[0]
      sig = str(row.get("second_chance_signal") or "")
      if not sig:
        return
      monitor.second_chance_signal = sig
      prob = row.get("second_chance_prob_up")
      if prob is not None and prob == prob:
        monitor.second_chance_prob_up = float(prob)
      open_prob = row.get("prob_up")
      if open_prob is not None and open_prob == open_prob:
        monitor.second_chance_open_prob = float(open_prob)
      monitor.second_chance_open_signal = str(row.get("signal") or pred.get("signal") or "")
      secs = row.get("second_chance_seconds_remaining")
      mins = int(secs) // 60 if secs is not None and secs == secs else None
      sc_pct = float(prob) * 100 if prob is not None and prob == prob else None
      open_pct = float(open_prob) * 100 if open_prob is not None and open_prob == open_prob else None
      if sc_pct is not None:
        monitor.second_chance_summary = (
          f"{sig.replace('2ND ', '')} outlook at t+4"
          + (f" ({mins}m left when logged)" if mins is not None else "")
          + f" — {sc_pct:.0f}% UP"
          + (f" vs {open_pct:.0f}% at open" if open_pct is not None else "")
        )
      else:
        monitor.second_chance_summary = sig
    except Exception as e:
      log.debug("2nd Chance preview failed: %s", e)

  def poll_brti(self) -> None:
    """Background refresh of live price (BRTI/ERTI or exchange)."""
    self.live_price_quote(fresh=True, asset="btc")
    if self._slot15m_enabled("eth"):
      self.live_price_quote(fresh=True, asset="eth")

  def status(self) -> dict[str, Any]:
    return self._status_for_asset("btc")

  def eth_status(self) -> dict[str, Any]:
    if not self._slot15m_enabled("eth"):
      return {"ok": False, "error": "ETH 15m disabled"}
    return self._status_for_asset("eth")

  def _status_for_asset(self, asset: str) -> dict[str, Any]:
    acfg = self._acfg_15m(asset)
    kalshi = self._kalshi_for(asset)
    storage = self.storage if asset == "btc" else self.eth_storage()
    predictor = self._predictor_for(asset)
    fetcher = self.fetcher if asset == "btc" else self.eth_fetcher()
    df_15m = storage.load("15m")
    df_1m = storage.load("1m")
    live = self.live_price_quote(fresh=True, asset=asset)
    live_tick: dict[str, Any] | None = None
    if live:
      live_tick = {
        "price": round(live.price, 2),
        "source": live.source,
        "age_sec": round(live.age_sec, 1) if live.age_sec is not None else None,
      }
    series = kalshi.series_ticker
    index_id = index_id_for_cfg(acfg)
    last_err = self.last_error if asset == "btc" else self.eth_last_error
    logs_path = Path(self._acfg_15m(asset).get("paths", {}).get("logs", "data/logs"))
    return {
      "asset": asset,
      "symbol": acfg["symbol"],
      "exchange": getattr(fetcher, "_exchange_id", None) or acfg.get("exchange"),
      "exchange_connected": fetcher.is_connected(),
      "model": "trained" if predictor.model else "baseline",
      "primary_timeframe": "15m",
      "candles_15m": len(df_15m),
      "candles_1m": len(df_1m),
      "min_candles_15m": self.min_candles,
      "lookback_hours": acfg.get("lookback_hours", 12),
      "slot_context": "1h + 4h (primary) + 12h",
      "volume_spike_window": f"{acfg.get('features', {}).get('volume_spike_window', 16)}×15m",
      "price_feed": kalshi.price_feed_label(),
      "settlement_reference": kalshi.settlement_reference_label(),
      "index_id": index_id,
      "live_tick": live_tick,
      "kalshi": kalshi.status(),
      "latest_candle_15m": df_15m["timestamp"].iloc[-1].isoformat() if not df_15m.empty else None,
      "horizon_minutes": self.horizon,
      "timezone": self.tz,
      "prediction_schedule": "every :00, :15, :30, :45 ET",
      "last_error": last_err,
      "scheduler_running": self._scheduler is not None and getattr(self._scheduler, "running", False),
      "data_dir": str(logs_path.parent),
      "series_ticker": series,
    }

  def _schedule_second_chance(self, scheduler) -> None:
    if not self.cfg.get("second_chance", {}).get("enabled", True):
      return
    scheduler.add_job(
      self.run_second_chance,
      CronTrigger(minute="4,19,34,49", timezone=self.tz),
      id="second_chance",
      max_instances=1,
    )
    scheduler.add_job(
      self.refit_second_chance_calibrator,
      "interval",
      hours=6,
      id="refit_second_chance_calibrator",
      max_instances=1,
    )

  def _schedule_slot15_bot(self, scheduler) -> None:
    acfg = self.cfg
    bot_cfg = (acfg.get("intra_slot") or {}).get("bot") or {}
    if bot_cfg.get("continuous_enabled", True):
      poll_sec = int(bot_cfg.get("poll_seconds", 5))
      scheduler.add_job(
        self.run_slot15_bot_continuous,
        "interval",
        seconds=poll_sec,
        id="slot15_bot_continuous",
        max_instances=1,
      )
    if self._slot15m_enabled("eth"):
      eth_cfg = self._eth_cfg or asset_cfg(self.cfg, "eth")
      ebot_cfg = (eth_cfg.get("intra_slot") or {}).get("bot") or bot_cfg
      if ebot_cfg.get("continuous_enabled", True):
        poll_sec = int(ebot_cfg.get("poll_seconds", 5))
        scheduler.add_job(
          self.run_eth_slot15_bot_continuous,
          "interval",
          seconds=poll_sec,
          id="eth_slot15_bot_continuous",
          max_instances=1,
        )

  def _schedule_predictions(self, scheduler) -> None:
    scheduler.add_job(
      self.run_prediction,
      CronTrigger(minute="0,15,30,45", timezone=self.tz),
      id="predict",
      max_instances=1,
    )
    if self._slot15m_enabled("eth"):
      scheduler.add_job(
        self.run_eth_prediction,
        CronTrigger(minute="0,15,30,45", timezone=self.tz),
        id="eth_predict",
        max_instances=1,
      )

  def start_background(self) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=self.tz)
    scheduler.add_job(self.fetch_and_store, "interval", minutes=1, id="fetch", max_instances=1)
    scheduler.add_job(self.resolve_outcomes, "interval", minutes=1, id="resolve", max_instances=1)
    self._schedule_predictions(scheduler)
    self._schedule_second_chance(scheduler)
    self._schedule_hourly(scheduler)
    self._schedule_slot15_bot(scheduler)
    scheduler.add_job(self.fetch_and_store, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=2), id="fetch_now")
    scheduler.add_job(self.run_prediction, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=8), id="predict_now")
    if self._slot15m_enabled("eth"):
      scheduler.add_job(
        self.run_eth_prediction,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=10),
        id="eth_predict_now",
      )
    scheduler.add_job(self.run_hourly_open_snapshot, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=12), id="hourly_open_now")
    scheduler.add_job(self.run_hourly_prediction, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=15), id="hourly_now")
    if asset_enabled(self.cfg, "eth"):
      scheduler.add_job(
        self.run_eth_hourly_open_snapshot,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=14),
        id="eth_hourly_open_now",
      )
      scheduler.add_job(
        self.run_eth_hourly_prediction,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=18),
        id="eth_hourly_now",
      )
    scheduler.add_job(self.run_second_chance, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=20), id="second_chance_now")
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
    tune_cfg = self.cfg.get("bot_auto_tune") or {}
    if tune_cfg.get("enabled", True):
      tune_hour = int(tune_cfg.get("hour", 3))
      tune_minute = int(tune_cfg.get("minute", 0))
      scheduler.add_job(
        self.run_bot_auto_tuning,
        CronTrigger(hour=str(tune_hour), minute=str(tune_minute), timezone=self.tz),
        id="bot_auto_tune",
        max_instances=1,
      )
      log.info("Bot auto-tune scheduled daily at %02d:%02d %s", tune_hour, tune_minute, self.tz)
    poll_sec = float(self.cfg.get("kalshi", {}).get("brti_poll_sec", 1))
    scheduler.add_job(self.poll_brti, "interval", seconds=poll_sec, id="brti_poll", max_instances=1)
    scheduler.add_job(self.poll_brti, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=1), id="brti_now")
    scheduler.start()
    self._scheduler = scheduler
    try:
      from src.trading.bot_bootstrap import bootstrap_paper_bots

      activated = bootstrap_paper_bots(self)
      if activated:
        log.info("Paper bots auto-enabled from PAPER_BOT_AUTO_ENABLE: %s", ", ".join(activated))
    except Exception as e:
      log.warning("Paper bot bootstrap skipped: %s", e)
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
