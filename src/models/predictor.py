from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.features.engineering import build_feature_matrix, feature_columns
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
    df_1m: pd.DataFrame,
    df_15m: pd.DataFrame | None = None,
  ) -> Prediction:
    features = build_feature_matrix(df_1m, df_15m, self.cfg, include_phase2=True)
    latest = features.iloc[-1]
    price = float(latest["close"])
    ts = latest["timestamp"]

    if self.model is not None:
      cols = self.feature_names or feature_columns(features)
      X = features[cols].iloc[[-1]].fillna(0)
      prob_up = float(self.model.predict_proba(X)[0, 1])
    else:
      prob_up = self._baseline_prob(features)

    prob_down = 1.0 - prob_up
    confidence = abs(prob_up - 0.5) * 2  # 0 at 50/50, 1 at 100/0

    # Expected move: rough estimate from recent volatility
    vol = features["return_1"].rolling(20).std().iloc[-1]
    if pd.isna(vol):
      vol = 0.001
    direction = 1 if prob_up >= 0.5 else -1
    expected_move = direction * vol * price * np.sqrt(self.cfg.get("prediction_horizon_minutes", 5))

    signal = self.edge.recommend(prob_up)

    snap = {c: float(latest[c]) for c in feature_columns(features) if c in latest and not pd.isna(latest[c])}

    return Prediction(
      timestamp=ts,
      price=price,
      prob_up=prob_up,
      prob_down=prob_down,
      confidence=confidence,
      expected_move=expected_move,
      signal=signal,
      features_snapshot=snap,
    )

  def format_output(self, pred: Prediction) -> str:
    lines = [
      f"Timestamp: {pred.timestamp}",
      f"BTC Price: ${pred.price:,.2f}",
      "",
      "Next 5 minutes:",
      f"  UP    {pred.prob_up * 100:.1f}%",
      f"  DOWN  {pred.prob_down * 100:.1f}%",
      "",
      f"Expected move: {'+' if pred.expected_move >= 0 else ''}${pred.expected_move:,.0f}",
      f"Confidence: {pred.confidence * 100:.0f}%",
      "",
      f"Recommendation: {pred.signal.value}",
    ]
    return "\n".join(lines)
