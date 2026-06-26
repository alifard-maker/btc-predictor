from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

from src.data.auxiliary import AuxiliaryStore
from src.features.engineering import add_label, build_feature_matrix, feature_columns, training_feature_columns
from src.features.labels import add_slot_label
from src.models.prob_calibration import ProbabilityCalibrator


def _make_model(model_type: str):
  model_type = model_type.lower()
  if model_type == "lightgbm":
    import lightgbm as lgb
    return lgb.LGBMClassifier(
      n_estimators=300,
      learning_rate=0.05,
      max_depth=6,
      num_leaves=31,
      subsample=0.8,
      colsample_bytree=0.8,
      random_state=42,
      verbose=-1,
    )
  if model_type == "xgboost":
    import xgboost as xgb
    return xgb.XGBClassifier(
      n_estimators=300,
      learning_rate=0.05,
      max_depth=6,
      subsample=0.8,
      colsample_bytree=0.8,
      random_state=42,
      eval_metric="logloss",
    )
  if model_type == "random_forest":
    return RandomForestClassifier(
      n_estimators=200,
      max_depth=10,
      min_samples_leaf=50,
      random_state=42,
      n_jobs=-1,
    )
  raise ValueError(f"Unknown model type: {model_type}")


class ModelTrainer:
  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.model = None
    self.feature_names: list[str] = []
    self.calibrator = ProbabilityCalibrator()

  def _auxiliary(self) -> dict[str, pd.DataFrame]:
    return AuxiliaryStore(self.cfg).load_all()

  def _context_1m(
    self,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame | None,
  ) -> pd.DataFrame | None:
    """Use 1m context only when it spans enough of the 15m history."""
    if df_1m is None or df_1m.empty or df_15m.empty:
      return None
    if len(df_1m) < 500:
      return None
    t15 = pd.to_datetime(df_15m["timestamp"], utc=True)
    t1 = pd.to_datetime(df_1m["timestamp"], utc=True)
    span_15 = (t15.max() - t15.min()).total_seconds()
    span_1 = (t1.max() - t1.min()).total_seconds()
    if span_15 <= 0 or span_1 / span_15 < 0.3:
      return None
    return df_1m

  def prepare_training_data(
    self,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame | None = None,
  ) -> tuple[pd.DataFrame, pd.Series]:
    horizon = self.cfg.get("prediction_horizon_minutes", 15)
    mcfg = self.cfg.get("model", {})
    features = build_feature_matrix(
      df_15m,
      self._context_1m(df_15m, df_1m),
      self.cfg,
      include_phase2=True,
      primary_timeframe="15m",
      auxiliary=self._auxiliary(),
    )
    if mcfg.get("slot_labels", True):
      features = add_slot_label(
        features,
        tz_name=self.cfg.get("timezone", "America/New_York"),
        horizon_minutes=horizon,
      )
    else:
      features = add_label(features, horizon_minutes=horizon, timeframe_minutes=15)
    cols = training_feature_columns(features)
    self.feature_names = cols

    clean = features.dropna(subset=cols + ["label"])
    X = clean[cols]
    y = clean["label"]
    return X, y

  def train(
    self,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame | None = None,
  ) -> dict[str, float]:
    X, y = self.prepare_training_data(df_15m, df_1m)
    min_samples = self.cfg.get("model", {}).get("min_train_samples", 1500)
    if len(X) < min_samples:
      raise ValueError(f"Need at least {min_samples} samples, got {len(X)}")

    split_ratio = self.cfg.get("model", {}).get("train_test_split", 0.2)
    split_idx = int(len(X) * (1 - split_ratio))
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    model_type = self.cfg.get("model", {}).get("type", "lightgbm")
    self.model = _make_model(model_type)
    self.model.fit(X_train, y_train)

    proba = self.model.predict_proba(X_test)[:, 1]
    preds = (proba >= 0.5).astype(int)

    metrics = {
      "accuracy": float(accuracy_score(y_test, preds)),
      "auc": float(roc_auc_score(y_test, proba)) if y_test.nunique() > 1 else 0.5,
      "brier": float(brier_score_loss(y_test, proba)),
      "log_loss": float(log_loss(y_test, proba)),
      "n_train": len(X_train),
      "n_test": len(X_test),
      "n_features": len(self.feature_names),
    }

    if self.cfg.get("model", {}).get("calibrate", True):
      if self.calibrator.fit(proba, y_test):
        cal = np.array([self.calibrator.transform(p) for p in proba])
        metrics["brier_calibrated"] = float(brier_score_loss(y_test, cal))
        metrics["calibrator_fitted"] = 1.0
      else:
        metrics["calibrator_fitted"] = 0.0

    return metrics

  def fit_calibrator_from_tracker(self, tracker) -> bool:
    """Refit isotonic calibrator from resolved DB predictions."""
    df = tracker.load_resolved()
    if df.empty or len(df) < 30:
      return False
    return self.calibrator.fit(df["prob_up"], df["outcome"])

  def cross_validate(self, df_15m: pd.DataFrame, df_1m: pd.DataFrame | None = None, n_splits: int = 5) -> dict:
    X, y = self.prepare_training_data(df_15m, df_1m)
    tscv = TimeSeriesSplit(n_splits=n_splits)
    model_type = self.cfg.get("model", {}).get("type", "lightgbm")
    scores = []

    for train_idx, test_idx in tscv.split(X):
      model = _make_model(model_type)
      model.fit(X.iloc[train_idx], y.iloc[train_idx])
      proba = model.predict_proba(X.iloc[test_idx])[:, 1]
      scores.append(roc_auc_score(y.iloc[test_idx], proba))

    return {"cv_auc_mean": float(np.mean(scores)), "cv_auc_std": float(np.std(scores))}

  def save(self, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
      "model": self.model,
      "features": self.feature_names,
      "calibrator": self.calibrator.to_dict(),
      "feature_version": 2,
    }, path)

  def load(self, path: str | Path) -> None:
    data = joblib.load(path)
    self.model = data["model"]
    self.feature_names = data["features"]
    if "calibrator" in data:
      self.calibrator = ProbabilityCalibrator.from_dict(data["calibrator"])
