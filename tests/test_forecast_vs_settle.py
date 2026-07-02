"""Tests for forecast-vs-settle read-only stats."""

from __future__ import annotations

import pandas as pd

from src.calibration.forecast_vs_settle import forecast_vs_settle_summary


def test_forecast_vs_settle_direction_accuracy():
  df = pd.DataFrame([
    {
      "logged_at": "2026-07-01T10:05:00+00:00",
      "reference_price": 100.0,
      "blended_mu": 102.0,
      "terminal_sigma": 2.0,
      "settle_brti": 103.0,
      "settlement_zone_low": 100.0,
      "settlement_zone_high": 104.0,
    },
    {
      "logged_at": "2026-07-01T11:05:00+00:00",
      "reference_price": 100.0,
      "blended_mu": 98.0,
      "terminal_sigma": 2.0,
      "settle_brti": 101.0,
      "settlement_zone_low": 97.0,
      "settlement_zone_high": 99.0,
    },
  ])
  out = forecast_vs_settle_summary(df)
  assert out["n_resolved"] == 2
  assert out["direction_accuracy"] == 0.5
  assert out["mean_abs_error_usd"] == 2.0
  assert out["in_zone_rate"] == 0.5
  assert len(out["recent"]) == 2
