"""Tests for backtest metrics and edge testing."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.metrics import bootstrap_ci, compute_metrics, max_drawdown
from src.experiments.edge_test import compare_variants, minimum_sample_size


def test_max_drawdown():
  equity = np.cumsum([1.0, -1.5, 0.5, 1.0])
  assert max_drawdown(equity) == 1.5


def test_compute_metrics_basic():
  trades = pd.DataFrame({
    "filled": [True, True, True, False],
    "pnl_usd": [1.0, -0.5, 0.5, 0.0],
  })
  m = compute_metrics(trades, n_bootstrap=200)
  assert m.n_trades == 4
  assert m.n_filled == 3
  assert abs(m.win_rate - 2 / 3) < 0.001
  assert m.total_pnl_usd == 1.0
  assert m.expectancy_ci_lower is not None


def test_bootstrap_ci_contains_mean():
  rng = np.random.default_rng(0)
  values = rng.normal(0.1, 0.5, size=100)
  lo, hi = bootstrap_ci(values, np.mean, n_bootstrap=500, rng=rng)
  assert lo <= values.mean() <= hi


def test_compare_variants_detects_difference():
  a = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
  b = np.array([-1.0, -1.0, -1.0, -1.0, -1.0])
  result = compare_variants(a, b, name_a="good", name_b="bad", n_permutations=500)
  assert result.mean_diff_usd == 2.0
  assert result.permutation_p_value < 0.05
  assert result.significant_at_05 is True


def test_minimum_sample_size_positive():
  n = minimum_sample_size(effect_usd=0.05, std_usd=0.20)
  assert n >= 30
