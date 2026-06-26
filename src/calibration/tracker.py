from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.db.store import PredictionStore, create_prediction_store
from src.features.slots import floor_to_15m


class CalibrationTracker:
  """Track whether predicted probabilities match actual outcomes."""

  def __init__(self, cfg: dict[str, Any] | str):
    if isinstance(cfg, dict):
      self.store: PredictionStore = create_prediction_store(cfg)
      self.tz = cfg.get("timezone", "America/New_York")
    else:
      # Backward compat: sqlite path string
      from src.db.store import SqlitePredictionStore
      self.store = SqlitePredictionStore(cfg)
      self.store.init()
      self.tz = "America/New_York"

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

  def _dedupe_by_slot(self, df: pd.DataFrame) -> pd.DataFrame:
    """One row per 15m slot — keep earliest log (closest to slot open)."""
    if df.empty:
      return df
    out = df.copy()
    out["_slot"] = pd.to_datetime(out["timestamp"], utc=True).apply(
      lambda t: floor_to_15m(t, self.tz)
    )
    sort_col = None
    for col in ("created_at", "resolved_at", "id"):
      if col in out.columns and out[col].notna().any():
        sort_col = col
        break
    if sort_col:
      if sort_col == "id":
        out = out.sort_values(sort_col)
      else:
        out["_sort"] = pd.to_datetime(out[sort_col], utc=True)
        out = out.sort_values("_sort")
    return out.drop_duplicates(subset=["_slot"], keep="first").drop(
      columns=["_slot", "_sort"], errors="ignore"
    )

  def _prediction_correct(self, df: pd.DataFrame) -> pd.Series:
    return (df["prob_up"] >= 0.5) == df["outcome"]

  def rolling_accuracy(self, windows_hours: list[int] | None = None) -> dict[str, dict[str, Any]]:
    """Correct/total counts for resolved predictions in each rolling time window."""
    windows_hours = windows_hours or [1, 2, 4, 12]
    empty = {f"{h}h": {"correct": 0, "total": 0, "accuracy": None} for h in windows_hours}
    df = self._dedupe_by_slot(self.load_resolved())
    if df.empty:
      return empty

    now = pd.Timestamp.now(tz="UTC")
    df = df.copy()
    df["_slot"] = pd.to_datetime(df["timestamp"], utc=True).apply(
      lambda t: floor_to_15m(t, self.tz)
    )
    correct = self._prediction_correct(df)

    out: dict[str, dict[str, Any]] = {}
    for h in windows_hours:
      mask = df["_slot"] >= (now - pd.Timedelta(hours=h))
      n = int(mask.sum())
      if n == 0:
        out[f"{h}h"] = {"correct": 0, "total": 0, "accuracy": None}
      else:
        c = int(correct[mask].sum())
        out[f"{h}h"] = {"correct": c, "total": n, "accuracy": float(c / n)}
    return out

  def calibration_report(self, n_bins: int = 10) -> pd.DataFrame:
    df = self._dedupe_by_slot(self.load_resolved())
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
    df = self._dedupe_by_slot(self.load_resolved())
    if df.empty:
      return {"n_resolved": 0, "rolling_accuracy": self.rolling_accuracy()}

    brier = ((df["prob_up"] - df["outcome"]) ** 2).mean()
    longs = df[df["signal"] == "LONG"]
    shorts = df[df["signal"] == "SHORT"]
    cal = self.calibration_report()

    return {
      "n_resolved": len(df),
      "brier_score": float(brier),
      "overall_accuracy": float(self._prediction_correct(df).mean()),
      "rolling_accuracy": self.rolling_accuracy(),
      "long_signals": len(longs),
      "long_accuracy": float(longs["outcome"].mean()) if len(longs) else None,
      "short_signals": len(shorts),
      "short_accuracy": float(1 - shorts["outcome"].mean()) if len(shorts) else None,
      "mean_calibration_error": float(cal["calibration_error"].mean()) if not cal.empty else None,
    }
