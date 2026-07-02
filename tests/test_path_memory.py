"""Tests for path memory features."""

from __future__ import annotations

import pandas as pd

from src.features.path_memory import apply_path_memory_adjustment, path_memory_from_1m


def test_path_memory_from_1m_basic():
  ts = pd.date_range("2026-07-01 14:00:00", periods=30, freq="1min", tz="UTC")
  closes = [100.0 + i * 0.05 for i in range(30)]
  df = pd.DataFrame({"timestamp": ts, "close": closes, "open": closes, "high": closes, "low": closes, "volume": 1.0})
  path = path_memory_from_1m(
    df,
    hour_open=ts[0],
    lock_price=100.0,
    current_price=101.2,
  )
  assert path["return_from_hour_open_pct"] is not None
  assert path["max_runup_pct"] is not None
  assert path["momentum_score"] is not None


def test_apply_path_memory_adjustment_nonlinear():
  path = {
    "return_from_hour_open_pct": 0.6,
    "max_drawdown_pct": -0.7,
    "recovery_ratio": 0.8,
  }
  mu, detail = apply_path_memory_adjustment(
    100.0,
    5.0,
    path,
    hours_left=0.4,
    cfg={"path_memory": {"momentum_weight": 0.35, "recovery_weight": 0.25}},
  )
  assert mu != 100.0
  assert detail["path_shift_usd"] != 0.0
