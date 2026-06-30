"""Backtest performance metrics with bootstrap confidence intervals."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd


@dataclass
class BacktestMetrics:
  n_trades: int
  n_filled: int
  fill_rate: float
  win_rate: float
  expectancy_usd: float
  total_pnl_usd: float
  sharpe_like: float
  max_drawdown_usd: float
  expectancy_ci_lower: float | None = None
  expectancy_ci_upper: float | None = None
  win_rate_ci_lower: float | None = None
  win_rate_ci_upper: float | None = None

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def max_drawdown(equity: np.ndarray) -> float:
  if len(equity) == 0:
    return 0.0
  peak = np.maximum.accumulate(equity)
  dd = peak - equity
  return float(np.max(dd)) if len(dd) else 0.0


def bootstrap_ci(
  values: np.ndarray | pd.Series,
  stat_fn: Callable[[np.ndarray], float],
  *,
  n_bootstrap: int = 2000,
  alpha: float = 0.05,
  rng: np.random.Generator | None = None,
) -> tuple[float, float]:
  arr = np.asarray(values, dtype=float)
  arr = arr[np.isfinite(arr)]
  if len(arr) == 0:
    return 0.0, 0.0
  rng = rng or np.random.default_rng(42)
  n = len(arr)
  stats = np.empty(n_bootstrap)
  for i in range(n_bootstrap):
    sample = arr[rng.integers(0, n, size=n)]
    stats[i] = stat_fn(sample)
  lo = float(np.percentile(stats, 100 * alpha / 2))
  hi = float(np.percentile(stats, 100 * (1 - alpha / 2)))
  return lo, hi


def compute_metrics(
  trades: pd.DataFrame,
  *,
  pnl_col: str = "pnl_usd",
  filled_col: str = "filled",
  n_bootstrap: int = 2000,
  alpha: float = 0.05,
) -> BacktestMetrics:
  if trades.empty:
    return BacktestMetrics(
      n_trades=0,
      n_filled=0,
      fill_rate=0.0,
      win_rate=0.0,
      expectancy_usd=0.0,
      total_pnl_usd=0.0,
      sharpe_like=0.0,
      max_drawdown_usd=0.0,
    )

  n_trades = len(trades)
  filled_mask = trades[filled_col] if filled_col in trades.columns else pd.Series(True, index=trades.index)
  filled = trades[filled_mask]
  n_filled = len(filled)
  fill_rate = n_filled / n_trades if n_trades else 0.0

  if n_filled == 0:
    return BacktestMetrics(
      n_trades=n_trades,
      n_filled=0,
      fill_rate=fill_rate,
      win_rate=0.0,
      expectancy_usd=0.0,
      total_pnl_usd=0.0,
      sharpe_like=0.0,
      max_drawdown_usd=0.0,
    )

  pnl = filled[pnl_col].astype(float).values
  wins = pnl > 0
  win_rate = float(wins.mean())
  expectancy = float(pnl.mean())
  total = float(pnl.sum())
  std = float(pnl.std(ddof=1)) if n_filled > 1 else 0.0
  sharpe = expectancy / std if std > 0 else 0.0
  equity = np.cumsum(pnl)
  mdd = max_drawdown(equity)

  exp_lo, exp_hi = bootstrap_ci(pnl, np.mean, n_bootstrap=n_bootstrap, alpha=alpha)
  wr_lo, wr_hi = bootstrap_ci(wins.astype(float), np.mean, n_bootstrap=n_bootstrap, alpha=alpha)

  return BacktestMetrics(
    n_trades=n_trades,
    n_filled=n_filled,
    fill_rate=round(fill_rate, 4),
    win_rate=round(win_rate, 4),
    expectancy_usd=round(expectancy, 4),
    total_pnl_usd=round(total, 4),
    sharpe_like=round(sharpe, 4),
    max_drawdown_usd=round(mdd, 4),
    expectancy_ci_lower=round(exp_lo, 4),
    expectancy_ci_upper=round(exp_hi, 4),
    win_rate_ci_lower=round(wr_lo, 4),
    win_rate_ci_upper=round(wr_hi, 4),
  )
