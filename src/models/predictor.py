from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.features.engineering import build_feature_matrix, feature_columns
from src.features.slots import floor_to_15m, slot_end, slot_label
from src.trading.edge import EdgeCalculator, Signal


@dataclass
class Prediction:
  timestamp: pd.Timestamp
  price: float
  prob_up: float
  prob_down: float
  confidence: float
  expected_move: float
  signal: Signal
  features_snapshot: dict[str, float]
  slot_start: pd.Timestamp | None = None
  slot_end: pd.Timestamp | None = None
  slot_label: str = ""


class Predictor:
  def __init__(self, cfg: dict[str, Any], model_path: str | None = None):
    self.cfg = cfg
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
    """Heuristic baseline when no trained model exists yet."""
    row = features.iloc[-1]
    score = 0.5

    if "momentum_5" in row and not pd.isna(row["momentum_5"]):
      score += np.clip(row["momentum_5"] * 10, -0.15, 0.15)
    if "rsi_norm" in row and not pd.isna(row["rsi_norm"]):
      score -= np.clip(row["rsi_norm"] * 0.1, -0.1, 0.1)
    if "vwap_distance" in row and not pd.isna(row["vwap_distance"]):
      score -= np.clip(row["vwap_distance"] * 2, -0.1, 0.1)
    if "volume_spike" in row and not pd.isna(row["volume_spike"]):
      mom = row.get("momentum_5", 0) or 0
      if row["volume_spike"] > 1.5:
        score += np.sign(mom) * 0.05

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
    price = float(latest["close"])
    ts = pd.Timestamp(latest["timestamp"])
    if ts.tzinfo is None:
      ts = ts.tz_localize("UTC")

    # Prediction is for the upcoming 15m slot starting at next boundary from candle time
    slot_s = floor_to_15m(ts)
    slot_e = slot_end(slot_s)

    if self.model is not None:
      cols = self.feature_names or feature_columns(features)
      X = features[cols].iloc[[-1]].fillna(0)
      prob_up = float(self.model.predict_proba(X)[0, 1])
    else:
      prob_up = self._baseline_prob(features)

    prob_down = 1.0 - prob_up
    confidence = abs(prob_up - 0.5) * 2
    horizon = self.cfg.get("prediction_horizon_minutes", 15)

    vol = features["return_1"].rolling(16).std().iloc[-1]
    if pd.isna(vol):
      vol = 0.002
    direction = 1 if prob_up >= 0.5 else -1
    expected_move = direction * vol * price * np.sqrt(horizon)

    signal = self.edge.recommend(prob_up)
    snap = {c: float(latest[c]) for c in feature_columns(features) if c in latest and not pd.isna(latest[c])}

    return Prediction(
      timestamp=slot_s,
      price=price,
      prob_up=prob_up,
      prob_down=prob_down,
      confidence=confidence,
      expected_move=expected_move,
      signal=signal,
      features_snapshot=snap,
      slot_start=slot_s,
      slot_end=slot_e,
      slot_label=slot_label(slot_s),
    )

  def format_output(self, pred: Prediction) -> str:
    horizon = self.cfg.get("prediction_horizon_minutes", 15)
    window = pred.slot_label or f"next {horizon} min"
    lines = [
      f"Slot: {window}",
      f"Timestamp: {pred.timestamp}",
      f"BTC Price: ${pred.price:,.2f}",
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
    return "\n".join(lines)
