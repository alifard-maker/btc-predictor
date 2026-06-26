from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.features.engineering import add_label, build_feature_matrix, feature_columns
from src.models.trainer import ModelTrainer, _make_model
from src.trading.edge import EdgeCalculator, Signal


class Backtester:
  """Stage 2: walk-forward backtest with fee-adjusted edge filtering."""

  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.edge = EdgeCalculator(cfg)
    self.horizon = cfg.get("prediction_horizon_minutes", 15)

  def run(
    self,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame | None = None,
    train_window: int = 2000,
    step: int = 200,
  ) -> pd.DataFrame:
    features = build_feature_matrix(df_15m, df_1m, self.cfg, include_phase2=True, primary_timeframe="15m")
    features = add_label(features, horizon_minutes=self.horizon, timeframe_minutes=15)
    cols = feature_columns(features)
    clean = features.dropna(subset=cols + ["label", "future_return"]).reset_index(drop=True)

    if len(clean) < train_window + step:
      raise ValueError(f"Need at least {train_window + step} 15m rows, got {len(clean)}")

    results = []
    trainer = ModelTrainer(self.cfg)

    for start in range(train_window, len(clean) - 1, step):
      train_slice = clean.iloc[start - train_window : start]
      test_slice = clean.iloc[start : start + step]

      X_train = train_slice[cols]
      y_train = train_slice["label"]
      trainer.feature_names = cols
      trainer.model = _make_model(trainer.cfg.get("model", {}).get("type", "lightgbm"))
      trainer.model.fit(X_train, y_train)

      X_test = test_slice[cols]
      probas = trainer.model.predict_proba(X_test)[:, 1]

      for i, (_, row) in enumerate(test_slice.iterrows()):
        prob_up = float(probas[i])
        signal = self.edge.recommend(prob_up)
        actual = int(row["label"])
        future_ret = float(row["future_return"])

        pnl = 0.0
        if signal == Signal.LONG:
          pnl = future_ret - self.edge.round_trip_cost
        elif signal == Signal.SHORT:
          pnl = -future_ret - self.edge.round_trip_cost

        results.append({
          "timestamp": row["timestamp"],
          "prob_up": prob_up,
          "signal": signal.value,
          "actual_up": actual,
          "future_return": future_ret,
          "pnl": pnl if signal != Signal.NO_TRADE else 0.0,
          "traded": signal != Signal.NO_TRADE,
        })

    return pd.DataFrame(results)

  def analyze(self, results: pd.DataFrame) -> dict[str, Any]:
    traded = results[results["traded"]]
    if traded.empty:
      return {"error": "No trades generated"}

    longs = traded[traded["signal"] == "LONG"]
    shorts = traded[traded["signal"] == "SHORT"]

    return {
      "total_trades": len(traded),
      "long_trades": len(longs),
      "short_trades": len(shorts),
      "long_win_rate": float(longs["actual_up"].mean()) if len(longs) else None,
      "short_win_rate": float(1 - shorts["actual_up"].mean()) if len(shorts) else None,
      "overall_win_rate": float(
        ((longs["actual_up"] == 1).sum() + (shorts["actual_up"] == 0).sum()) / len(traded)
      ),
      "total_pnl_pct": float(traded["pnl"].sum() * 100),
      "avg_pnl_per_trade_pct": float(traded["pnl"].mean() * 100),
      "beats_50pct": float(
        ((longs["actual_up"] == 1).sum() + (shorts["actual_up"] == 0).sum()) / len(traded)
      ) > 0.5 + self.edge.round_trip_cost,
      "sharpe_approx": float(traded["pnl"].mean() / traded["pnl"].std()) if traded["pnl"].std() > 0 else 0,
    }
