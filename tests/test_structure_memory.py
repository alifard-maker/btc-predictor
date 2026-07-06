"""Tests for structure-memory μ/σ adjustment."""

from __future__ import annotations

import pandas as pd

from src.backtest.structure_memory import (
  StructureMemoryConfig,
  adjust_mu_sigma_from_structure,
  structure_blocks_yes_above,
)


def _flat_df(n: int = 12, base: float = 63_700.0) -> pd.DataFrame:
  rows = []
  for i in range(n):
    lo = base - 50
    hi = base + 50
    rows.append({"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=i), "open": base, "high": hi, "low": lo, "close": base, "volume": 100})
  return pd.DataFrame(rows)


def test_tight_box_pulls_mu_toward_mid():
  df = _flat_df(12, 63_750.0)
  mu, sigma, detail = adjust_mu_sigma_from_structure(
    64_200.0,
    80.0,
    63_780.0,
    df,
    cfg=StructureMemoryConfig(lookback_bars=12),
  )
  assert detail.get("applied")
  assert mu < 64_200.0


def test_upper_box_blocks_yes_above():
  df = _flat_df(12, 63_750.0)
  _, _, detail = adjust_mu_sigma_from_structure(
    64_100.0,
    80.0,
    63_790.0,
    df,
    cfg=StructureMemoryConfig(lookback_bars=8),
  )
  assert structure_blocks_yes_above(detail) or detail.get("pos_in_box", 0) > 0.5
