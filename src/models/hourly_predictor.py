"""Hourly Kalshi predictor — ML + structure blend with regime and logging."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.data.auxiliary import AuxiliaryStore
from src.features.engineering import build_feature_matrix, training_feature_columns
from src.models.daily_predictor import DailyPredictor
from src.models.hourly_range_log import (
  RANGE_BE_PREFIX,
  RANGE_ML_PREFIX,
  contract_to_row_prefix,
  lean_bands_from_contracts,
  serialize_lean_bands,
)
from src.models.prob_calibration import ProbabilityCalibrator
from src.trading.contract_signals import BUY_NO, BUY_YES, VALUE_YES, is_actionable_buy
from src.trading.hourly_bet_assessment import assess_hourly_bet
from src.trading.hourly_regime import HourlyRegimeFilter

log = logging.getLogger(__name__)


class HourlyPredictor:
  """Blend 1h ML direction with structure terminal distribution for Kalshi contracts."""

  def __init__(self, cfg: dict[str, Any], *, asset: str = "btc"):
    self.cfg = cfg
    self.asset = asset
    self.structure = DailyPredictor(cfg, daily_cfg=cfg.get("daily"))
    self.regime = HourlyRegimeFilter(cfg)
    self.hcfg = cfg.get("hourly", {})
    self.ml_weight = float(self.hcfg.get("blend", {}).get("ml_weight", 0.6))
    self.structure_weight = float(self.hcfg.get("blend", {}).get("structure_weight", 0.4))
    self.model = None
    self.feature_names: list[str] = []
    self.calibrator = ProbabilityCalibrator()
    self._load_model()
    self._sigma_scale = self._load_sigma_scale()

  def _model_path(self) -> Path:
    return Path(self.cfg["paths"]["models"]) / "model_hourly.joblib"

  def _sigma_path(self) -> Path:
    return Path(self.cfg["paths"]["logs"]) / "hourly_sigma_calibration.json"

  def _load_model(self) -> None:
    path = self._model_path()
    if not path.exists():
      return
    try:
      from src.models.hourly_trainer import HourlyModelTrainer
      trainer = HourlyModelTrainer(self.cfg)
      trainer.load(path)
      self.model = trainer.model
      self.feature_names = trainer.feature_names
      self.calibrator = trainer.calibrator
      log.info("Loaded hourly ML model (%d features)", len(self.feature_names))
    except Exception as e:
      log.warning("Hourly model load failed: %s", e)

  def _load_sigma_scale(self) -> float:
    path = self._sigma_path()
    if not path.exists():
      return 1.0
    try:
      data = json.loads(path.read_text())
      return float(data.get("sigma_scale", 1.0))
    except Exception:
      return 1.0

  def save_sigma_scale(self, scale: float) -> None:
    path = self._sigma_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sigma_scale": scale, "updated_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    self._sigma_scale = scale

  def _prob_15m_aggregate(self, calibration_tracker) -> float | None:
    try:
      df = calibration_tracker.load_recent(8)
      if df.empty or "prob_up" not in df.columns:
        return None
      return float(pd.to_numeric(df["prob_up"], errors="coerce").tail(4).mean())
    except Exception:
      return None

  def _ml_prob_up(
    self,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame | None,
    prob_15m_avg: float | None,
  ) -> tuple[float | None, float | None]:
    if self.model is None or df_1h.empty:
      return None, None
    try:
      features = build_feature_matrix(
        df_1h,
        None,
        self.cfg,
        include_phase2=True,
        primary_timeframe="1h",
        auxiliary=AuxiliaryStore(self.cfg).load_all(),
      )
      if prob_15m_avg is not None:
        features["prob_15m_aggregate"] = prob_15m_avg
      cols = [c for c in self.feature_names if c in features.columns]
      row = features.iloc[-1]
      if any(pd.isna(row.get(c)) for c in cols):
        return None, None
      X = pd.DataFrame([{c: row[c] for c in cols}])
      prob = float(self.model.predict_proba(X)[:, 1][0])
      if self.calibrator.fitted:
        prob = float(self.calibrator.transform(prob))
      return prob, prob
    except Exception as e:
      log.warning("Hourly ML inference failed: %s", e)
      return None, None

  def _mu_from_prob(self, price: float, prob_up: float, sigma: float) -> float:
    z = (prob_up - 0.5) * 2.0
    return price + z * sigma * 0.55

  def predict(
    self,
    *,
    current_price: float,
    df_1h: pd.DataFrame | None,
    df_15m: pd.DataFrame | None = None,
    calibration_tracker=None,
  ) -> dict[str, Any]:
    prob_15m_avg = self._prob_15m_aggregate(calibration_tracker) if calibration_tracker else None
    ml_prob, _ = self._ml_prob_up(df_1h if df_1h is not None else pd.DataFrame(), df_15m, prob_15m_avg)

    kalshi_book = self.structure.markets.active_book(reference_price=current_price)
    structure_out = self.structure.predict(
      current_price=current_price,
      df_1h=df_1h,
      book=kalshi_book,
    )
    if not structure_out.get("ok"):
      return structure_out

    structure_mu = float(structure_out["terminal_mu"])
    structure_sigma = float(structure_out["terminal_sigma"]) * self._sigma_scale
    hours_left = float(structure_out["hours_to_settle"])

    ml_mu = self._mu_from_prob(current_price, ml_prob, structure_sigma) if ml_prob is not None else None
    if ml_mu is not None:
      blended_mu = self.ml_weight * ml_mu + self.structure_weight * structure_mu
      method = "blend"
    else:
      blended_mu = structure_mu
      method = "structure"

    blended = self.structure.predict(
      current_price=current_price,
      df_1h=df_1h,
      book=kalshi_book,
      override_mu=blended_mu,
      override_sigma=structure_sigma,
    )
    if not blended.get("ok"):
      return blended

    range_ml = (blended.get("strategy_range") or {}).get("most_likely")
    thresh_be = (blended.get("strategy_threshold") or {}).get("best_edge")
    thresh_ml = (blended.get("strategy_threshold") or {}).get("most_likely")
    pick = range_ml
    if thresh_be and self.structure._row_near_forecast(thresh_be, blended_mu, structure_sigma):
      if thresh_be.get("signal") in (BUY_YES, BUY_NO, VALUE_YES, "LEAN YES", "LEAN NO"):
        pick = thresh_be
    elif thresh_ml and self.structure._row_near_forecast(thresh_ml, blended_mu, structure_sigma):
      pick = thresh_ml

    prob = float(pick.get("model_prob", 0.5)) if pick else 0.5
    edge = pick.get("edge") if pick else None
    signal = str(pick.get("signal", "NEUTRAL")) if pick else "NEUTRAL"
    confidence = abs(prob - 0.5) * 2.0
    expected_move_pct = (blended_mu - current_price) / current_price * 100 if current_price > 0 else 0.0

    compression = None
    box = blended.get("structure", {}).get("consolidation")
    if box:
      compression = box.get("tightness")

    regime = self.regime.evaluate(
      expected_move_pct=expected_move_pct,
      hours_to_settle=hours_left,
      sigma_pct=structure_sigma / current_price * 100 if current_price > 0 else 0,
      edge=edge,
      compression=compression,
    )
    if not regime.allow_trade and is_actionable_buy(signal):
      signal = "NEUTRAL"

    direction = "UP" if prob >= 0.55 else ("DOWN" if prob <= 0.45 else "NEUTRAL")
    if pick and pick.get("strike_type") == "greater":
      direction = "ABOVE" if prob >= 0.5 else "BELOW"
    elif pick and pick.get("strike_type") == "less":
      direction = "BELOW" if prob >= 0.5 else "ABOVE"

    blended["method"] = method
    blended["ml_prob_up"] = round(ml_prob, 4) if ml_prob is not None else None
    blended["ml_mu"] = round(ml_mu, 2) if ml_mu is not None else None
    blended["structure_mu"] = round(structure_mu, 2)
    blended["blended_mu"] = round(blended_mu, 2)
    blended["terminal_mu"] = round(blended_mu, 2)
    blended["terminal_sigma"] = round(structure_sigma, 2)
    blended["confidence"] = round(confidence, 4)
    blended["direction"] = direction
    blended["prob_15m_avg"] = round(prob_15m_avg, 4) if prob_15m_avg is not None else None
    blended["regime"] = {"allow_trade": regime.allow_trade, "reasons": regime.reasons}
    blended["primary_pick"] = pick
    from src.trading.hourly_regime import max_hours_to_settle_for_manual_entry

    blended["max_hours_to_settle_for_entry"] = float(max_hours_to_settle_for_manual_entry(self.cfg))
    # Manual lane never pauses on settle timing — UI/API buys always allowed.
    blended["manual_entry"] = {
      "allowed": True,
      "block_reason": None,
      "hours_to_settle": round(hours_left, 2) if hours_left is not None else None,
      "max_hours_to_settle_for_entry": blended["max_hours_to_settle_for_entry"],
    }
    hrcfg = self.hcfg.get("regime", {})
    blended["bet_assessment"] = assess_hourly_bet(
      signal=pick.get("signal") if pick else "NEUTRAL",
      edge=edge,
      regime_allow_trade=regime.allow_trade,
      regime_reasons=regime.reasons,
      expected_move_pct=expected_move_pct,
      min_edge=float(hrcfg.get("min_edge", 0.05)),
      min_expected_move_pct=float(hrcfg.get("min_expected_move_pct", 0.12)),
    )
    return blended

  def to_log_row(self, pred: dict[str, Any]) -> dict[str, Any]:
    ev = pred.get("event") or {}
    pick = pred.get("primary_pick") or {}
    ml = pred.get("most_likely", {}).get("threshold") or {}
    ml_block = pred.get("most_likely") or {}
    sr = pred.get("strategy_range") or {}
    now = datetime.now(timezone.utc).isoformat()
    regime = pred.get("regime") or {}
    row = {
      "logged_at": now,
      "event_ticker": ev.get("event_ticker", ""),
      "frequency": ev.get("frequency", "hourly"),
      "settle_time": ev.get("close_time", now),
      "series_ticker": ev.get("series_ticker"),
      "title": ev.get("title"),
      "reference_price": pred.get("current_price"),
      "terminal_mu": pred.get("terminal_mu"),
      "terminal_sigma": pred.get("terminal_sigma"),
      "ml_prob_up": pred.get("ml_prob_up"),
      "ml_mu": pred.get("ml_mu"),
      "structure_mu": pred.get("structure_mu"),
      "blended_mu": pred.get("blended_mu"),
      "hours_to_settle": pred.get("hours_to_settle"),
      "primary_ticker": pick.get("ticker"),
      "primary_type": pick.get("contract_type", "threshold"),
      "primary_label": pick.get("label"),
      "primary_strike_type": pick.get("strike_type"),
      "primary_floor": pick.get("floor_strike"),
      "primary_cap": pick.get("cap_strike"),
      "primary_model_prob": pick.get("model_prob"),
      "primary_kalshi_mid": pick.get("kalshi_mid"),
      "primary_edge": pick.get("edge"),
      "primary_signal": pick.get("signal"),
      "most_likely_label": ml.get("label"),
      "most_likely_prob": ml.get("model_prob"),
      "confidence": pred.get("confidence"),
      "expected_move_pct": (pred.get("blended_mu", 0) - pred.get("current_price", 0)) / pred.get("current_price", 1) * 100,
      "direction": pred.get("direction"),
      "method": pred.get("method"),
      "regime_blocked": 0 if regime.get("allow_trade", True) else 1,
      "regime_notes": "; ".join(regime.get("reasons") or []),
      "prob_15m_avg": pred.get("prob_15m_avg"),
      "settlement_zone_low": ml_block.get("settlement_zone_low"),
      "settlement_zone_high": ml_block.get("settlement_zone_high"),
    }
    row.update(contract_to_row_prefix(sr.get("most_likely"), RANGE_ML_PREFIX))
    row.update(contract_to_row_prefix(sr.get("best_edge"), RANGE_BE_PREFIX))
    row["range_lean_bands"] = serialize_lean_bands(lean_bands_from_contracts(sr.get("contracts")))
    row["asset"] = self.asset
    return row
