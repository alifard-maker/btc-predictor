"""Train 1h LightGBM for hourly Kalshi settlement direction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

from src.data.auxiliary import AuxiliaryStore
from src.features.engineering import build_feature_matrix, training_feature_columns
from src.features.hourly_labels import add_hourly_label
from src.models.prob_calibration import ProbabilityCalibrator
from src.models.trainer import _make_model


class HourlyModelTrainer:
  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.model = None
    self.feature_names: list[str] = []
    self.calibrator = ProbabilityCalibrator()

  def prepare_training_data(
    self,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame | None = None,
  ) -> tuple[pd.DataFrame, pd.Series]:
    hcfg = self.cfg.get("hourly", {})
    features = build_feature_matrix(
      df_1h,
      None,
      self.cfg,
      include_phase2=True,
      primary_timeframe="1h",
      auxiliary=AuxiliaryStore(self.cfg).load_all(),
    )
    if df_15m is not None and not df_15m.empty and hcfg.get("use_15m_aggregate", True):
      df15 = df_15m.copy()
      df15["timestamp"] = pd.to_datetime(df15["timestamp"], utc=True)
      df15 = df15.sort_values("timestamp")
      df15["prob_15m_proxy"] = (df15["close"].pct_change() > 0).astype(float).rolling(4).mean()
      feat_ts = pd.to_datetime(features["timestamp"], utc=True)
      prox = df15.set_index("timestamp")["prob_15m_proxy"]
      features["prob_15m_aggregate"] = [
        float(prox.loc[:t].tail(4).mean()) if len(prox.loc[:t]) else 0.5
        for t in feat_ts
      ]
    features = add_hourly_label(features, tz_name=self.cfg.get("timezone", "America/New_York"))
    cols = training_feature_columns(features)
    if "prob_15m_aggregate" in features.columns:
      cols = cols + ["prob_15m_aggregate"]
    self.feature_names = cols
    clean = features.dropna(subset=cols + ["label"])
    return clean[cols], clean["label"]

  def train(self, df_1h: pd.DataFrame, df_15m: pd.DataFrame | None = None) -> dict[str, float]:
    X, y = self.prepare_training_data(df_1h, df_15m)
    min_samples = int(self.cfg.get("hourly", {}).get("min_train_samples", 500))
    if len(X) < min_samples:
      raise ValueError(f"Need at least {min_samples} hourly samples, got {len(X)}")

    split_ratio = float(self.cfg.get("hourly", {}).get("train_test_split", 0.2))
    split_idx = int(len(X) * (1 - split_ratio))
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    model_type = self.cfg.get("hourly", {}).get("model_type", self.cfg.get("model", {}).get("type", "lightgbm"))
    self.model = _make_model(model_type)
    self.model.fit(X_train, y_train)
    proba = self.model.predict_proba(X_test)[:, 1]
    metrics = {
      "accuracy": float(accuracy_score(y_test, (proba >= 0.5).astype(int))),
      "auc": float(roc_auc_score(y_test, proba)) if y_test.nunique() > 1 else 0.5,
      "brier": float(brier_score_loss(y_test, proba)),
      "n_train": len(X_train),
      "n_test": len(X_test),
      "n_features": len(self.feature_names),
    }
    if self.cfg.get("hourly", {}).get("calibrate", True):
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
    self.feature_names = data["features"]
    if "calibrator" in data:
      self.calibrator = ProbabilityCalibrator.from_dict(data["calibrator"])
