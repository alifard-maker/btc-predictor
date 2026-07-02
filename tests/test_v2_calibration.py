"""Tests for V2 backtest calibration."""

from __future__ import annotations

import pandas as pd

from src.calibration.v2_calibration import (
  V2CalibrationParams,
  calibrate_v2_from_backtest,
  generate_poll_forecast_rows,
  score_forecast_rows,
)


def _synthetic_1h(n: int = 200) -> pd.DataFrame:
  ts = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
  base = 50_000.0
  closes = base + pd.Series(range(n)).astype(float) * 5.0
  return pd.DataFrame({
    "timestamp": ts,
    "open": closes.shift(1).fillna(base),
    "high": closes + 80,
    "low": closes - 80,
    "close": closes,
    "volume": 1.0,
  })


def test_v2_calibration_params_normalize():
  p = V2CalibrationParams(path_weight=0.8, structure_weight=0.8).normalized_blend()
  assert abs(p.path_weight + p.structure_weight - 1.0) < 1e-9


def test_generate_poll_forecast_rows():
  df = _synthetic_1h(80)
  params = V2CalibrationParams(path_weight=0.35, structure_weight=0.65)
  rows = generate_poll_forecast_rows(df, params, warmup_bars=10, sample_stride=2)
  assert len(rows) > 0
  assert set(rows["poll_idx"].unique()) == {0, 1, 2, 3}


def test_calibrate_v2_from_backtest_smoke():
  from src.calibration.v2_calibration import V2CalibrationSearch

  df = _synthetic_1h(180)
  cfg = {"paths": {"logs": "data/logs"}, "hourly_v2": {}}
  result = calibrate_v2_from_backtest(
    cfg,
    df,
    train_frac=0.7,
    sample_stride=3,
    validate_mechanics=False,
    search=V2CalibrationSearch(
      path_weights=[0.0, 0.35],
      momentum_weights=[0.25, 0.35],
      recovery_weights=[0.15, 0.25],
      shock_thresholds=[0.5],
    ),
  )
  assert "calibrated_params" in result
  assert result["holdout"]["calibrated"]["intrahour_polls"]["n"] > 0
