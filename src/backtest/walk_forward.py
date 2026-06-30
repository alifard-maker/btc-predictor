"""Walk-forward backtest engine — rolling train/test with no lookahead."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np
import pandas as pd

from src.backtest.fee_model import FeeModel
from src.backtest.fill_simulator import FillSimulator, OrderStyle
from src.backtest.metrics import BacktestMetrics, compute_metrics
from src.data.auxiliary import AuxiliaryStore
from src.features.engineering import add_label, build_feature_matrix, training_feature_columns
from src.features.hourly_labels import add_hourly_label
from src.features.labels import add_slot_label
from src.models.trainer import ModelTrainer, _make_model
from src.trading.edge import EdgeCalculator, Signal


@dataclass
class WalkForwardConfig:
  train_window: int = 500
  test_window: int = 50
  step: int = 50
  horizon: str = "hourly"  # "hourly" | "15m"
  order_style: OrderStyle = OrderStyle.PASSIVE_LIMIT
  time_to_settle_hours: float = 1.0
  volume_proxy: float = 1.0
  bootstrap_samples: int = 2000
  bootstrap_alpha: float = 0.05
  rng_seed: int | None = 42

  @classmethod
  def from_config(cls, cfg: dict[str, Any]) -> WalkForwardConfig:
    raw = cfg.get("backtest", {})
    style = raw.get("order_style", "passive_limit")
    return cls(
      train_window=int(raw.get("train_window", 500)),
      test_window=int(raw.get("test_window", 50)),
      step=int(raw.get("step", 50)),
      horizon=str(raw.get("horizon", "hourly")),
      order_style=OrderStyle(style) if style in OrderStyle._value2member_map_ else OrderStyle.PASSIVE_LIMIT,
      time_to_settle_hours=float(raw.get("time_to_settle_hours", 1.0)),
      volume_proxy=float(raw.get("volume_proxy", 1.0)),
      bootstrap_samples=int(raw.get("bootstrap_samples", 2000)),
      bootstrap_alpha=float(raw.get("bootstrap_alpha", 0.05)),
      rng_seed=raw.get("rng_seed", 42),
    )


def generate_folds(
  n_samples: int,
  train_window: int,
  test_window: int,
  step: int,
) -> Iterator[tuple[int, int, int]]:
  """Yield (train_start, train_end, test_end) indices with no lookahead."""
  start = train_window
  while start + test_window <= n_samples:
    yield start - train_window, start, start + test_window
    start += step


class WalkForwardBacktest:
  """Rolling ML walk-forward with simulated Kalshi fills and fees."""

  def __init__(self, cfg: dict[str, Any], wf_cfg: WalkForwardConfig | None = None):
    self.cfg = cfg
    self.wf = wf_cfg or WalkForwardConfig.from_config(cfg)
    self.edge = EdgeCalculator(cfg)
    self.fees = FeeModel(cfg=cfg)
    self.fills = FillSimulator(app_cfg=cfg, fee_model=self.fees)
    self.fills._rng = np.random.default_rng(self.wf.rng_seed)

  def _prepare_features(
    self,
    df_primary: pd.DataFrame,
    df_context: pd.DataFrame | None,
  ) -> pd.DataFrame:
    primary_tf = "1h" if self.wf.horizon == "hourly" else "15m"
    aux = AuxiliaryStore(self.cfg).load_all()
    features = build_feature_matrix(
      df_primary,
      df_context,
      self.cfg,
      include_phase2=True,
      primary_timeframe=primary_tf,
      auxiliary=aux,
    )
    if self.wf.horizon == "hourly":
      features = add_hourly_label(features, tz_name=self.cfg.get("timezone", "America/New_York"))
    elif self.cfg.get("model", {}).get("slot_labels", True):
      horizon = self.cfg.get("prediction_horizon_minutes", 15)
      features = add_slot_label(
        features,
        tz_name=self.cfg.get("timezone", "America/New_York"),
        horizon_minutes=horizon,
      )
    else:
      horizon = self.cfg.get("prediction_horizon_minutes", 15)
      features = add_label(features, horizon_minutes=horizon, timeframe_minutes=15)
    return features

  def run(
    self,
    df_primary: pd.DataFrame,
    df_context: pd.DataFrame | None = None,
  ) -> tuple[pd.DataFrame, BacktestMetrics, list[dict[str, Any]]]:
    features = self._prepare_features(df_primary, df_context)
    cols = training_feature_columns(features)
    clean = features.dropna(subset=cols + ["label"]).reset_index(drop=True)

    min_needed = self.wf.train_window + self.wf.test_window
    if len(clean) < min_needed:
      raise ValueError(f"Need at least {min_needed} rows, got {len(clean)}")

    trades: list[dict[str, Any]] = []
    fold_summaries: list[dict[str, Any]] = []
    trainer = ModelTrainer(self.cfg)

    for fold_i, (train_start, train_end, test_end) in enumerate(
      generate_folds(len(clean), self.wf.train_window, self.wf.test_window, self.wf.step)
    ):
      train_slice = clean.iloc[train_start:train_end]
      test_slice = clean.iloc[train_end:test_end]

      X_train = train_slice[cols]
      y_train = train_slice["label"]
      trainer.feature_names = cols
      trainer.model = _make_model(trainer.cfg.get("model", {}).get("type", "lightgbm"))
      trainer.model.fit(X_train, y_train)

      X_test = test_slice[cols]
      probas = trainer.model.predict_proba(X_test)[:, 1]
      fold_pnl: list[float] = []

      for i, (_, row) in enumerate(test_slice.iterrows()):
        prob_up = float(probas[i])
        signal = self.edge.recommend(prob_up)
        actual_up = int(row["label"])

        if signal == Signal.NO_TRADE:
          trades.append(self._no_trade_row(row, prob_up, fold_i))
          continue

        side = "yes" if signal == Signal.LONG else "no"
        fill = self.fills.simulate_entry(
          prob_up=prob_up,
          side=side,
          order_style=self.wf.order_style,
          time_to_settle_hours=self.wf.time_to_settle_hours,
          volume_proxy=self.wf.volume_proxy,
        )

        won = (side == "yes" and actual_up == 1) or (side == "no" and actual_up == 0)
        pnl_usd = 0.0
        if fill.filled and fill.price_cents is not None:
          pnl_usd = self.fees.settlement_pnl_usd(
            side=side,
            entry_price_cents=fill.price_cents,
            contracts=fill.contracts,
            won=won,
            entry_maker=fill.is_maker,
          )
          fold_pnl.append(pnl_usd)

        trades.append({
          "timestamp": row["timestamp"],
          "fold": fold_i,
          "prob_up": prob_up,
          "signal": signal.value,
          "side": side,
          "actual_up": actual_up,
          "won": won,
          "filled": fill.filled,
          "fill_probability": fill.fill_probability,
          "entry_price_cents": fill.price_cents,
          "contracts": fill.contracts,
          "is_maker": fill.is_maker,
          "pnl_usd": pnl_usd,
          "skip_reason": fill.skip_reason,
        })

      fold_summaries.append({
        "fold": fold_i,
        "train_start": int(train_start),
        "train_end": int(train_end),
        "test_end": int(test_end),
        "n_test_signals": int((test_slice.index >= 0).sum()),
        "n_trades": sum(1 for t in trades if t.get("fold") == fold_i and t.get("signal") != Signal.NO_TRADE.value),
        "fold_pnl_usd": round(sum(fold_pnl), 4),
      })

    trade_df = pd.DataFrame(trades)
    metrics = compute_metrics(
      trade_df,
      n_bootstrap=self.wf.bootstrap_samples,
      alpha=self.wf.bootstrap_alpha,
    )
    return trade_df, metrics, fold_summaries

  @staticmethod
  def _no_trade_row(row: pd.Series, prob_up: float, fold: int) -> dict[str, Any]:
    return {
      "timestamp": row["timestamp"],
      "fold": fold,
      "prob_up": prob_up,
      "signal": Signal.NO_TRADE.value,
      "side": None,
      "actual_up": int(row["label"]),
      "won": None,
      "filled": False,
      "fill_probability": 0.0,
      "entry_price_cents": None,
      "contracts": 0,
      "is_maker": False,
      "pnl_usd": 0.0,
      "skip_reason": "no_trade",
    }
