"""Hypothesis testing for strategy edge — compare variants on OOS windows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.metrics import bootstrap_ci


@dataclass
class EdgeTestResult:
  variant_a: str
  variant_b: str
  n_pairs: int
  mean_diff_usd: float
  permutation_p_value: float
  bootstrap_ci_lower: float
  bootstrap_ci_upper: float
  significant_at_05: bool
  power_warning: str | None
  min_sample_recommended: int

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def _align_pnl(a: pd.Series, b: pd.Series) -> tuple[np.ndarray, np.ndarray]:
  """Align two PnL series on shared index (timestamps or fold ids)."""
  if isinstance(a, pd.Series) and isinstance(b, pd.Series):
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if joined.shape[1] >= 2:
      return joined.iloc[:, 0].values.astype(float), joined.iloc[:, 1].values.astype(float)
  arr_a = np.asarray(a, dtype=float)
  arr_b = np.asarray(b, dtype=float)
  n = min(len(arr_a), len(arr_b))
  return arr_a[:n], arr_b[:n]


def minimum_sample_size(
  effect_usd: float,
  std_usd: float,
  *,
  alpha: float = 0.05,
  power: float = 0.8,
) -> int:
  """Approximate n for two-sample mean difference (normal approx)."""
  if std_usd <= 0 or effect_usd <= 0:
    return 100
  from math import ceil, sqrt

  z_alpha = 1.96 if alpha == 0.05 else 2.576
  z_beta = 0.84 if power == 0.8 else 1.28
  n = ceil(((z_alpha + z_beta) * std_usd / effect_usd) ** 2)
  return max(30, int(n))


def permutation_test_mean_diff(
  a: np.ndarray,
  b: np.ndarray,
  *,
  n_permutations: int = 5000,
  rng: np.random.Generator | None = None,
) -> tuple[float, float]:
  """Return (observed_mean_diff, two-sided p-value)."""
  rng = rng or np.random.default_rng(42)
  a = a[np.isfinite(a)]
  b = b[np.isfinite(b)]
  n = min(len(a), len(b))
  if n == 0:
    return 0.0, 1.0
  a, b = a[:n], b[:n]
  observed = float(np.mean(a) - np.mean(b))
  combined = np.concatenate([a, b])
  n_a = len(a)
  count = 0
  for _ in range(n_permutations):
    rng.shuffle(combined)
    perm_a = combined[:n_a]
    perm_b = combined[n_a:]
    perm_diff = float(np.mean(perm_a) - np.mean(perm_b))
    if abs(perm_diff) >= abs(observed):
      count += 1
  p_value = (count + 1) / (n_permutations + 1)
  return observed, p_value


def compare_variants(
  pnl_a: pd.Series | np.ndarray,
  pnl_b: pd.Series | np.ndarray,
  *,
  name_a: str = "A",
  name_b: str = "B",
  n_permutations: int = 5000,
  alpha: float = 0.05,
  target_power: float = 0.8,
) -> EdgeTestResult:
  """Compare strategy variant A vs B on the same out-of-sample windows."""
  arr_a, arr_b = _align_pnl(
    pd.Series(pnl_a) if not isinstance(pnl_a, pd.Series) else pnl_a,
    pd.Series(pnl_b) if not isinstance(pnl_b, pd.Series) else pnl_b,
  )
  n_pairs = len(arr_a)
  mean_diff, p_value = permutation_test_mean_diff(arr_a, arr_b, n_permutations=n_permutations)
  lo, hi = bootstrap_ci(arr_a - arr_b, np.mean, alpha=alpha)

  pooled_std = float(np.std(np.concatenate([arr_a, arr_b]), ddof=1)) if n_pairs > 1 else 1.0
  min_n = minimum_sample_size(abs(mean_diff) if mean_diff != 0 else 0.01, pooled_std, power=target_power)

  power_warning = None
  if n_pairs < min_n:
    power_warning = (
      f"Only {n_pairs} paired observations; recommend ≥{min_n} for "
      f"80% power to detect ${abs(mean_diff):.4f} mean difference."
    )

  return EdgeTestResult(
    variant_a=name_a,
    variant_b=name_b,
    n_pairs=n_pairs,
    mean_diff_usd=round(mean_diff, 4),
    permutation_p_value=round(p_value, 4),
    bootstrap_ci_lower=round(lo, 4),
    bootstrap_ci_upper=round(hi, 4),
    significant_at_05=p_value < alpha,
    power_warning=power_warning,
    min_sample_recommended=min_n,
  )
