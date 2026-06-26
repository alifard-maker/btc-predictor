"""Train LightGBM for 2nd Chance reassessment at t+4min."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

from src.features.engineering import build_feature_matrix
from src.features.second_chance_labels import (
  build_second_chance_training_rows,
  second_chance_feature_columns,
)
from src.features.slots import floor_to_15m
from src.models.prob_calibration import ProbabilityCalibrator
from src.models.trainer import _make_model


class SecondChanceTrainer:
  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.model = None
    self.feature_names: list[str] = second_chance_feature_columns()
    self.calibrator = ProbabilityCalibrator()

  def _open_probs_from_main_model(
    self,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame | None,
    main_model,
    feature_names: list[str],
    calibrator: ProbabilityCalibrator | None,
  ) -> pd.Series:
    """Retrospective open prob at each slot boundary for training context."""
    if main_model is None or df_15m.empty:
      return pd.Series(dtype=float)
    tz = self.cfg.get("timezone", "America/New_York")
    features = build_feature_matrix(
      df_15m,
      df_1m,
      self.cfg,
      include_phase2=True,
      primary_timeframe="15m",
    )
    features["timestamp"] = pd.to_datetime(features["timestamp"], utc=True)
    features["_slot"] = features["timestamp"].apply(lambda t: floor_to_15m(t, tz))
    slot_rows = features.sort_values("timestamp").groupby("_slot", as_index=False).last()
    cols = [c for c in (feature_names or []) if c in slot_rows.columns]
    if not cols:
      return pd.Series(dtype=float)
    probs: dict[str, float] = {}
    for _, row in slot_rows.iterrows():
      if any(pd.isna(row.get(c)) for c in cols):
        continue
      X = pd.DataFrame([{c: row[c] for c in cols}])
      p = float(main_model.predict_proba(X)[:, 1][0])
      if calibrator and calibrator.fitted:
        p = float(calibrator.transform(p))
      probs[pd.Timestamp(row["_slot"]).isoformat()] = p
    return pd.Series(probs)

  def prepare_training_data(
    self,
    df_1m: pd.DataFrame,
    df_15m: pd.DataFrame | None = None,
    *,
    main_model=None,
    main_feature_names: list[str] | None = None,
    main_calibrator: ProbabilityCalibrator | None = None,
  ) -> tuple[pd.DataFrame, pd.Series]:
    scfg = self.cfg.get("second_chance", {})
    elapsed = float(scfg.get("elapsed_minutes", 4))
    open_probs = None
    if df_15m is not None and not df_15m.empty and main_model is not None:
      open_probs = self._open_probs_from_main_model(
        df_15m, df_1m, main_model, main_feature_names or [], main_calibrator
      )
    rows = build_second_chance_training_rows(
      df_1m,
      tz_name=self.cfg.get("timezone", "America/New_York"),
      elapsed_minutes=elapsed,
      open_probs=open_probs,
    )
    if rows.empty:
      return pd.DataFrame(), pd.Series(dtype=int)
    cols = second_chance_feature_columns()
    clean = rows.dropna(subset=cols + ["label"])
    return clean[cols], clean["label"]

  def train(
    self,
    df_1m: pd.DataFrame,
    df_15m: pd.DataFrame | None = None,
    *,
    main_model=None,
    main_feature_names: list[str] | None = None,
    main_calibrator: ProbabilityCalibrator | None = None,
  ) -> dict[str, float]:
    X, y = self.prepare_training_data(
      df_1m,
      df_15m,
      main_model=main_model,
      main_feature_names=main_feature_names,
      main_calibrator=main_calibrator,
    )
    min_samples = int(self.cfg.get("second_chance", {}).get("min_train_samples", 300))
    if len(X) < min_samples:
      raise ValueError(f"Need at least {min_samples} 2nd Chance samples, got {len(X)}")

    split_ratio = float(self.cfg.get("second_chance", {}).get("train_test_split", 0.2))
    split_idx = int(len(X) * (1 - split_ratio))
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    model_type = self.cfg.get("second_chance", {}).get(
      "model_type", self.cfg.get("model", {}).get("type", "lightgbm")
    )
    self.model = _make_model(model_type)
    self.model.fit(X_train, y_train)
    self.feature_names = list(X.columns)
    proba = self.model.predict_proba(X_test)[:, 1]
    metrics = {
      "accuracy": float(accuracy_score(y_test, (proba >= 0.5).astype(int))),
      "auc": float(roc_auc_score(y_test, proba)) if y_test.nunique() > 1 else 0.5,
      "brier": float(brier_score_loss(y_test, proba)),
      "n_train": len(X_train),
      "n_test": len(X_test),
      "n_features": len(self.feature_names),
    }
    if self.cfg.get("second_chance", {}).get("calibrate", True):
      if self.calibrator.fit(proba, y_test):
        cal = np.array([self.calibrator.transform(p) for p in proba])
        metrics["brier_calibrated"] = float(brier_score_loss(y_test, cal))
    return metrics

  def save(self, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
      {"model": self.model, "features": self.feature_names, "calibrator": self.calibrator.to_dict()},
      path,
    )

  def load(self, path: str | Path) -> None:
    data = joblib.load(path)
    self.model = data["model"]
    self.feature_names = data.get("features") or second_chance_feature_columns()
    if "calibrator" in data:
      self.calibrator = ProbabilityCalibrator.from_dict(data["calibrator"])
