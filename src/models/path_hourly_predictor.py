"""Hourly V2 predictor — structure + nonlinear path memory (no v1 ML blend)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.features.path_memory import apply_path_memory_adjustment, path_memory_from_1m
from src.models.daily_predictor import DailyPredictor
from src.models.hourly_predictor import HourlyPredictor
from src.models.hourly_range_log import (
  RANGE_BE_PREFIX,
  RANGE_ML_PREFIX,
  contract_to_row_prefix,
  lean_bands_from_contracts,
  serialize_lean_bands,
)
from src.trading.contract_signals import BUY_NO, BUY_YES, VALUE_YES, is_actionable_buy
from src.trading.hourly_bet_assessment import assess_hourly_bet
from src.trading.hourly_regime import HourlyRegimeFilter

log = logging.getLogger(__name__)


class PathHourlyPredictor:
  """V2 hourly predictor: Kalshi structure + intrahour path memory (fully separate from v1 ML)."""

  def __init__(self, cfg: dict[str, Any], *, asset: str = "btc"):
    self.cfg = cfg
    self.asset = asset
    self.hcfg = cfg.get("hourly_v2", {})
    self.structure = DailyPredictor(cfg, daily_cfg=cfg.get("daily"))
    self.regime = HourlyRegimeFilter(self._regime_cfg())
    self.path_weight = float(self.hcfg.get("blend", {}).get("path_weight", 0.55))
    self.structure_weight = float(self.hcfg.get("blend", {}).get("structure_weight", 0.45))
    self._sigma_scale = 1.0

  def _regime_cfg(self) -> dict[str, Any]:
    import copy

    c = copy.deepcopy(self.cfg)
    c.setdefault("hourly", {})["regime"] = {
      **c.get("hourly", {}).get("regime", {}),
      **self.hcfg.get("regime", {}),
    }
    return c

  def predict(
    self,
    *,
    current_price: float,
    df_1h: pd.DataFrame | None,
    df_15m: pd.DataFrame | None = None,
    df_1m: pd.DataFrame | None = None,
    lock_price: float | None = None,
    calibration_tracker=None,
  ) -> dict[str, Any]:
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

    path = path_memory_from_1m(
      df_1m,
      lock_price=lock_price or current_price,
      current_price=current_price,
      tz_name=self.cfg.get("timezone", "America/New_York"),
    )
    path_mu, path_detail = apply_path_memory_adjustment(
      structure_mu,
      structure_sigma,
      path,
      hours_left,
      cfg=self.hcfg,
    )
    blended_mu = self.path_weight * path_mu + self.structure_weight * structure_mu

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

    blended["method"] = "path_v2"
    blended["ml_prob_up"] = None
    blended["ml_mu"] = round(path_mu, 2)
    blended["structure_mu"] = round(structure_mu, 2)
    blended["blended_mu"] = round(blended_mu, 2)
    blended["terminal_mu"] = round(blended_mu, 2)
    blended["terminal_sigma"] = round(structure_sigma, 2)
    blended["confidence"] = round(confidence, 4)
    blended["direction"] = direction
    blended["prob_15m_avg"] = None
    blended["path_memory"] = path
    blended["path_detail"] = path_detail
    blended["regime"] = {"allow_trade": regime.allow_trade, "reasons": regime.reasons}
    blended["primary_pick"] = pick
    hrcfg = self.hcfg.get("regime", {}) or self.cfg.get("hourly", {}).get("regime", {})
    blended["bet_assessment"] = assess_hourly_bet(
      signal=pick.get("signal") if pick else "NEUTRAL",
      edge=edge,
      regime_allow_trade=regime.allow_trade,
      regime_reasons=regime.reasons,
      expected_move_pct=expected_move_pct,
      min_edge=float(hrcfg.get("min_edge", 0.05)),
      min_expected_move_pct=float(hrcfg.get("min_expected_move_pct", 0.12)),
    )
    blended["predictor_version"] = "v2_path"
    return blended

  def to_log_row(self, pred: dict[str, Any]) -> dict[str, Any]:
    row = HourlyPredictor(self.cfg, asset=self.asset).to_log_row(pred)
    row["method"] = "path_v2"
    row["ml_prob_up"] = None
    path = pred.get("path_memory") or {}
    detail = pred.get("path_detail") or {}
    notes = {
      "predictor": "v2_path",
      "path": path,
      "path_detail": detail,
    }
    regime = pred.get("regime") or {}
    reasons = list(regime.get("reasons") or [])
    row["regime_notes"] = json.dumps(notes, default=str)
    if reasons:
      row["regime_notes"] = row["regime_notes"] + " | " + "; ".join(reasons)
    row["asset"] = self.asset
    return row
