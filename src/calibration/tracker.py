from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.db.store import PredictionStore, create_prediction_store


class CalibrationTracker:
  """Track whether predicted probabilities match actual outcomes."""

  def __init__(self, cfg: dict[str, Any] | str):
    if isinstance(cfg, dict):
      self.store: PredictionStore = create_prediction_store(cfg)
    else:
      # Backward compat: sqlite path string
      from src.db.store import SqlitePredictionStore
      self.store = SqlitePredictionStore(cfg)
      self.store.init()

  def log_prediction(
    self, timestamp: str, price: float, prob_up: float, prob_down: float,
    confidence: float, signal: str, expected_move: float,
  ) -> int:
    return self.store.log_prediction(
      timestamp, price, prob_up, prob_down, confidence, signal, expected_move
    )

  def get_pending(self) -> list[tuple[int, str, float]]:
    return self.store.get_pending()

  def resolve_with_prices(self, price_lookup: dict[str, tuple[float, float]]) -> int:
    return self.store.resolve_with_prices(price_lookup)

  def load_resolved(self) -> pd.DataFrame:
    return self.store.load_resolved()

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    return self.store.load_recent(limit)

  def latest(self) -> dict[str, Any] | None:
    return self.store.latest()

  def calibration_report(self, n_bins: int = 10) -> pd.DataFrame:
    df = self.load_resolved()
    if df.empty or len(df) < n_bins:
      return pd.DataFrame()

    df["bin"] = pd.cut(df["prob_up"], bins=n_bins, labels=False)
    report = df.groupby("bin", observed=True).agg(
      count=("outcome", "count"),
      mean_predicted=("prob_up", "mean"),
      mean_actual=("outcome", "mean"),
      accuracy=("outcome", lambda x: ((df.loc[x.index, "prob_up"] >= 0.5) == x).mean()),
    ).reset_index()

    report["calibration_error"] = (report["mean_predicted"] - report["mean_actual"]).abs()
    return report

  def summary(self) -> dict[str, Any]:
    df = self.load_resolved()
    if df.empty:
      return {"n_resolved": 0}

    brier = ((df["prob_up"] - df["outcome"]) ** 2).mean()
    longs = df[df["signal"] == "LONG"]
    shorts = df[df["signal"] == "SHORT"]
    cal = self.calibration_report()

    return {
      "n_resolved": len(df),
      "brier_score": float(brier),
      "overall_accuracy": float(((df["prob_up"] >= 0.5) == df["outcome"]).mean()),
      "long_signals": len(longs),
      "long_accuracy": float(longs["outcome"].mean()) if len(longs) else None,
      "short_signals": len(shorts),
      "short_accuracy": float(1 - shorts["outcome"].mean()) if len(shorts) else None,
      "mean_calibration_error": float(cal["calibration_error"].mean()) if not cal.empty else None,
    }
