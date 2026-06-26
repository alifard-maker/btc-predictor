from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from src.features.engineering import build_feature_matrix, feature_columns
from src.features.slots import (
  floor_to_15m,
  reference_price_at_slot,
  slot_end,
  slot_label,
)
from src.trading.edge import EdgeCalculator, Signal


@dataclass
class Prediction:
  timestamp: pd.Timestamp
  price: float  # reference price at t=0 (slot open)
  prob_up: float
  prob_down: float
  confidence: float
  expected_move: float
  signal: Signal
  features_snapshot: dict[str, float]
  slot_start: pd.Timestamp | None = None
  slot_end: pd.Timestamp | None = None
  slot_label: str = ""
  reference_price: float = 0.0
  current_price: float | None = None


class Predictor:
  def __init__(self, cfg: dict[str, Any], model_path: str | None = None):
    self.cfg = cfg
    self.tz = cfg.get("timezone", "America/New_York")
    self.edge = EdgeCalculator(cfg)
    self.model = None
    self.feature_names: list[str] = []

    if model_path:
      self.load_model(model_path)

  def load_model(self, path: str) -> None:
    import joblib
    data = joblib.load(path)
    self.model = data["model"]
    self.feature_names = data["features"]

  def _baseline_prob(self, features: pd.DataFrame) -> float:
    row = features.iloc[-1]
    score = 0.5

    def _bump(key: str, scale: float, cap: float = 0.15) -> None:
      nonlocal score
      if key in row and not pd.isna(row[key]):
        score += np.clip(float(row[key]) * scale, -cap, cap)

    # 1h momentum — strongest signal for next 15m
    _bump("momentum_4", 14, 0.18)

    # 2h momentum — secondary confirmation
    _bump("momentum_8", 5, 0.08)

    # 1h slot context (last hour of 15m bars)
    _bump("slot_return_1h", 10, 0.12)
    if "slot_up_ratio_1h" in row and not pd.isna(row["slot_up_ratio_1h"]):
      score += np.clip((float(row["slot_up_ratio_1h"]) - 0.5) * 0.35, -0.08, 0.08)

    # 4h slot context (emphasized medium-term)
    _bump("slot_return_4h", 6, 0.10)
    if "slot_up_ratio" in row and not pd.isna(row["slot_up_ratio"]):
      score += np.clip((float(row["slot_up_ratio"]) - 0.5) * 0.22, -0.06, 0.06)

    # 12h regime — light mean-reversion fade on extended moves
    _bump("slot_return_12h", -3, 0.06)

    if "rsi_norm" in row and not pd.isna(row["rsi_norm"]):
      score -= np.clip(float(row["rsi_norm"]) * 0.1, -0.1, 0.1)
    if "vwap_distance" in row and not pd.isna(row["vwap_distance"]):
      score -= np.clip(float(row["vwap_distance"]) * 2, -0.1, 0.1)
    if "volume_spike" in row and not pd.isna(row["volume_spike"]):
      mom = float(row.get("momentum_4", 0) or 0)
      if row["volume_spike"] > 1.5:
        score += np.sign(mom) * 0.07
      elif row["volume_spike"] > 1.2 and abs(mom) > 0.001:
        score += np.sign(mom) * 0.03
    return float(np.clip(score, 0.05, 0.95))

  def predict(
    self,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame | None = None,
  ) -> Prediction:
    min_candles = self.cfg.get("min_candles_15m", 30)
    if len(df_15m) < min_candles:
      raise ValueError(f"Need at least {min_candles} fifteen-minute candles, got {len(df_15m)}")

    features = build_feature_matrix(df_15m, df_1m, self.cfg, include_phase2=True, primary_timeframe="15m")
    latest = features.iloc[-1]
    current_price = float(latest["close"])

    # Slot for the upcoming/current 15m interval in ET
    now_utc = pd.Timestamp(datetime.now(timezone.utc))
    slot_s = floor_to_15m(now_utc, self.tz)
    slot_e = slot_end(slot_s, self.tz)

    ref_price = reference_price_at_slot(df_1m, slot_s, fallback=current_price)

    if self.model is not None:
      cols = self.feature_names or feature_columns(features)
      X = features[cols].iloc[[-1]].fillna(0)
      prob_up = float(self.model.predict_proba(X)[0, 1])
    else:
      prob_up = self._baseline_prob(features)

    prob_down = 1.0 - prob_up
    confidence = abs(prob_up - 0.5) * 2
    horizon = self.cfg.get("prediction_horizon_minutes", 15)

    lookback = self.cfg.get("features", {}).get(
      "expected_move_vol_candles",
      self.cfg.get("features", {}).get("slot_lookback_candles", 16),
    )
    vol = features["return_1"].rolling(lookback).std().iloc[-1]
    if pd.isna(vol):
      vol = 0.002
    direction = 1 if prob_up >= 0.5 else -1
    expected_move = direction * vol * ref_price * np.sqrt(horizon)

    signal = self.edge.recommend(prob_up)
    snap = {c: float(latest[c]) for c in feature_columns(features) if c in latest and not pd.isna(latest[c])}

    return Prediction(
      timestamp=slot_s,
      price=ref_price,
      reference_price=ref_price,
      current_price=current_price,
      prob_up=prob_up,
      prob_down=prob_down,
      confidence=confidence,
      expected_move=expected_move,
      signal=signal,
      features_snapshot=snap,
      slot_start=slot_s,
      slot_end=slot_e,
      slot_label=slot_label(slot_s, self.tz),
    )

  def format_output(self, pred: Prediction) -> str:
    horizon = self.cfg.get("prediction_horizon_minutes", 15)
    window = pred.slot_label or f"next {horizon} min"
    lines = [
      f"Slot: {window}",
      f"Reference price (t=0): ${pred.reference_price:,.2f}",
      f"Current price: ${pred.current_price:,.2f}" if pred.current_price else "",
      "",
      f"Next {horizon} minutes:",
      f"  UP    {pred.prob_up * 100:.1f}%",
      f"  DOWN  {pred.prob_down * 100:.1f}%",
      "",
      f"Expected move: {'+' if pred.expected_move >= 0 else ''}${pred.expected_move:,.0f}",
      f"Confidence: {pred.confidence * 100:.0f}%",
      "",
      f"Recommendation: {pred.signal.value}",
    ]
    return "\n".join(line for line in lines if line != "")
