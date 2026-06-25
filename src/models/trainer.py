from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

from src.features.engineering import add_label, build_feature_matrix, feature_columns


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

  def prepare_training_data(
    self,
    df_1m: pd.DataFrame,
    df_15m: pd.DataFrame | None = None,
  ) -> tuple[pd.DataFrame, pd.Series]:
    horizon = self.cfg.get("prediction_horizon_minutes", 5)
    features = build_feature_matrix(df_1m, df_15m, self.cfg, include_phase2=True)
    features = add_label(features, horizon_minutes=horizon)
    cols = feature_columns(features)
    self.feature_names = cols

    clean = features.dropna(subset=cols + ["label"])
    X = clean[cols]
    y = clean["label"]
    return X, y

  def train(
    self,
    df_1m: pd.DataFrame,
    df_15m: pd.DataFrame | None = None,
  ) -> dict[str, float]:
    X, y = self.prepare_training_data(df_1m, df_15m)
    min_samples = self.cfg.get("model", {}).get("min_train_samples", 10000)
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
    }
    return metrics

  def cross_validate(self, df_1m: pd.DataFrame, df_15m: pd.DataFrame | None = None, n_splits: int = 5) -> dict:
    X, y = self.prepare_training_data(df_1m, df_15m)
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
    joblib.dump({"model": self.model, "features": self.feature_names}, path)

  def load(self, path: str | Path) -> None:
    data = joblib.load(path)
    self.model = data["model"]
    self.feature_names = data["features"]
