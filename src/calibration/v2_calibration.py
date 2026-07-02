"""V2 path-hourly calibration — fit blend/path weights and sigma scale from backtest rows."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.hourly_mechanics_backtest import (
  _blended_mu_for_poll,
  _brti_poll_prices,
  run_mechanics_backtest,
)
from src.calibration.forecast_vs_settle import _direction_correct

log = logging.getLogger(__name__)


@dataclass
class V2CalibrationParams:
  path_weight: float = 0.55
  structure_weight: float = 0.45
  momentum_weight: float = 0.35
  recovery_weight: float = 0.25
  shock_threshold_pct: float = 0.5
  sigma_scale: float = 1.0

  def normalized_blend(self) -> V2CalibrationParams:
    pw = max(0.0, min(1.0, float(self.path_weight)))
    sw = max(0.0, float(self.structure_weight))
    total = pw + sw
    if total <= 0:
      return V2CalibrationParams(path_weight=0.0, structure_weight=1.0)
    return V2CalibrationParams(
      path_weight=pw / total,
      structure_weight=sw / total,
      momentum_weight=self.momentum_weight,
      recovery_weight=self.recovery_weight,
      shock_threshold_pct=self.shock_threshold_pct,
      sigma_scale=self.sigma_scale,
    )

  def to_path_memory_cfg(self) -> dict[str, float]:
    return {
      "momentum_weight": float(self.momentum_weight),
      "recovery_weight": float(self.recovery_weight),
      "shock_threshold_pct": float(self.shock_threshold_pct),
    }

  def apply_to_hcfg(self, hcfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(hcfg)
    out["blend"] = {
      **(hcfg.get("blend") or {}),
      "path_weight": self.path_weight,
      "structure_weight": self.structure_weight,
    }
    out["path_memory"] = {
      **(hcfg.get("path_memory") or {}),
      **self.to_path_memory_cfg(),
    }
    out["_v2_calibration"] = {
      "sigma_scale": self.sigma_scale,
      "applied": True,
    }
    return out


def _bundled_calibration_path(asset: str = "btc") -> Path:
  root = Path(__file__).resolve().parents[2]
  if asset == "btc":
    return root / "data" / "calibration" / "hourly_v2_calibration.json"
  return root / "data" / "calibration" / f"hourly_v2_calibration_{asset}.json"


def calibration_path(cfg: dict[str, Any], *, asset: str = "btc") -> Path:
  logs = Path(cfg["paths"]["logs"])
  if asset == "btc":
    runtime = logs / "hourly_v2_calibration.json"
  else:
    runtime = logs / f"hourly_v2_calibration_{asset}.json"
  if runtime.is_file():
    return runtime
  return _bundled_calibration_path(asset)


def save_calibration_path(cfg: dict[str, Any], *, asset: str = "btc") -> Path:
  logs = Path(cfg["paths"]["logs"])
  if asset == "btc":
    return logs / "hourly_v2_calibration.json"
  return logs / f"hourly_v2_calibration_{asset}.json"


def load_v2_calibration(cfg: dict[str, Any], *, asset: str = "btc") -> V2CalibrationParams | None:
  path = calibration_path(cfg, asset=asset)
  if not path.exists():
    return None
  try:
    data = json.loads(path.read_text())
    params = data.get("params") or data
    return V2CalibrationParams(
      path_weight=float(params.get("path_weight", 0.55)),
      structure_weight=float(params.get("structure_weight", 0.45)),
      momentum_weight=float(params.get("momentum_weight", 0.35)),
      recovery_weight=float(params.get("recovery_weight", 0.25)),
      shock_threshold_pct=float(params.get("shock_threshold_pct", 0.5)),
      sigma_scale=float(params.get("sigma_scale", 1.0)),
    ).normalized_blend()
  except Exception as exc:
    log.warning("Failed to load V2 calibration %s: %s", path, exc)
    return None


def save_v2_calibration(
  cfg: dict[str, Any],
  params: V2CalibrationParams,
  *,
  asset: str = "btc",
  meta: dict[str, Any] | None = None,
) -> Path:
  path = save_calibration_path(cfg, asset=asset)
  path.parent.mkdir(parents=True, exist_ok=True)
  payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "asset": asset,
    "params": asdict(params.normalized_blend()),
    **(meta or {}),
  }
  path.write_text(json.dumps(payload, indent=2) + "\n")
  return path


def _momentum_4h(opens: np.ndarray, idx: int) -> float:
  mom_idx = max(0, idx - 4)
  if opens[mom_idx] <= 0:
    return 0.0
  return (float(opens[idx]) - float(opens[mom_idx])) / float(opens[mom_idx]) * 100.0


def _sigma_from_bar(open_px: float, high: float, low: float) -> float:
  return max(30.0, (high - low) * 0.6 + open_px * 0.001)


def _fast_v2_blended_mu(
  *,
  open_px: float,
  momentum_4h_pct: float,
  poll_prices: list[float],
  poll_idx: int,
  brti: float,
  sigma_raw: float,
  params: V2CalibrationParams,
) -> float:
  """Lightweight μ for calibration grid search (no DataFrame churn)."""
  structure_mu = open_px * (1.0 + momentum_4h_pct / 100.0 * 0.25)
  effective_sigma = sigma_raw * params.sigma_scale
  hours_left = max(0.08, 1.0 - poll_idx * 0.22)
  time_weight = max(0.0, min(1.0, 1.0 - max(0.0, hours_left)))

  ret_open = (brti / open_px - 1.0) * 100.0 if open_px > 0 else 0.0
  closes = poll_prices[: poll_idx + 1]
  cum = [(c / open_px - 1.0) * 100.0 for c in closes if open_px > 0]
  max_dd = float(min(cum)) if cum else 0.0
  trough = float(min(closes)) if closes else brti
  recovery = None
  if max_dd < -params.shock_threshold_pct and trough < brti and open_px > trough:
    recovery = float((brti - trough) / (open_px - trough))

  shock = max(params.shock_threshold_pct, 0.1)
  momentum_term = float(np.tanh(ret_open / shock) * effective_sigma * params.momentum_weight)
  recovery_term = 0.0
  if recovery is not None and max_dd < -params.shock_threshold_pct:
    recovery_term = (float(recovery) - 0.5) * effective_sigma * params.recovery_weight
  shift = momentum_term * (0.45 + 0.55 * time_weight) + recovery_term
  path_mu = structure_mu + shift
  return params.path_weight * path_mu + params.structure_weight * structure_mu


def _rows_from_hours(
  hours: list[dict[str, Any]],
  params: V2CalibrationParams,
  poll_indices: tuple[int, ...],
) -> pd.DataFrame:
  rows: list[dict[str, Any]] = []
  for h in hours:
    for poll_idx in poll_indices:
      brti = h["polls"][poll_idx]
      mu = _fast_v2_blended_mu(
        open_px=h["open_px"],
        momentum_4h_pct=h["mom"],
        poll_prices=h["polls"],
        poll_idx=poll_idx,
        brti=brti,
        sigma_raw=h["sigma_raw"],
        params=params,
      )
      rows.append({
        "timestamp": h["timestamp"],
        "poll_idx": poll_idx,
        "reference_price": h["open_px"],
        "settle_brti": h["close"],
        "blended_mu": mu,
        "sigma_raw": h["sigma_raw"],
      })
  return pd.DataFrame(rows)


def _precompute_hours(
  df_1h: pd.DataFrame,
  *,
  warmup_bars: int,
  sample_stride: int,
  max_hours: int | None = None,
) -> list[dict[str, Any]]:
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  opens = df["open"].astype(float).values
  highs = df["high"].astype(float).values
  lows = df["low"].astype(float).values
  closes = df["close"].astype(float).values
  ts = pd.to_datetime(df["timestamp"], utc=True)
  hours: list[dict[str, Any]] = []
  for i in range(warmup_bars, len(df), max(1, sample_stride)):
    open_px = float(opens[i])
    hours.append({
      "timestamp": ts.iloc[i],
      "open_px": open_px,
      "high": float(highs[i]),
      "low": float(lows[i]),
      "close": float(closes[i]),
      "mom": _momentum_4h(opens, i),
      "sigma_raw": _sigma_from_bar(open_px, float(highs[i]), float(lows[i])),
      "polls": _brti_poll_prices(open_px, float(highs[i]), float(lows[i]), float(closes[i])),
    })
    if max_hours is not None and len(hours) >= max_hours:
      break
  return hours


def _cfg_with_params(cfg: dict[str, Any], params: V2CalibrationParams) -> dict[str, Any]:
  out = dict(cfg)
  hcfg = dict(cfg.get("hourly_v2") or {})
  out["hourly_v2"] = params.apply_to_hcfg(hcfg)
  return out


def generate_poll_forecast_rows(
  df_1h: pd.DataFrame,
  params: V2CalibrationParams,
  *,
  warmup_bars: int = 24,
  sample_stride: int = 1,
  poll_indices: tuple[int, ...] | None = None,
) -> pd.DataFrame:
  """One row per (hour, poll) with μ forecast and settle for scoring."""
  cfg_stub: dict[str, Any] = {"hourly_v2": params.apply_to_hcfg({})}
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  opens = df["open"].astype(float).values
  highs = df["high"].astype(float).values
  lows = df["low"].astype(float).values
  closes = df["close"].astype(float).values
  ts = pd.to_datetime(df["timestamp"], utc=True)
  poll_indices = poll_indices if poll_indices is not None else (0, 1, 2, 3)

  rows: list[dict[str, Any]] = []
  for i in range(warmup_bars, len(df), max(1, sample_stride)):
    open_px = float(opens[i])
    high = float(highs[i])
    low = float(lows[i])
    close = float(closes[i])
    mom = _momentum_4h(opens, i)
    sigma_raw = _sigma_from_bar(open_px, high, low)
    sigma = sigma_raw * params.sigma_scale
    polls = _brti_poll_prices(open_px, high, low, close)
    hour_ts = ts.iloc[i].to_pydatetime()
    for poll_idx in poll_indices:
      brti = polls[poll_idx]
      mu = _blended_mu_for_poll(
        mu_mode="v2_path",
        open_px=open_px,
        high=high,
        low=low,
        close=close,
        hour_open=open_px,
        momentum_4h_pct=mom,
        poll_idx=poll_idx,
        poll_prices=polls,
        brti=brti,
        hour_ts=hour_ts,
        sigma=sigma,
        cfg=cfg_stub,
      )
      rows.append({
        "timestamp": ts.iloc[i],
        "poll_idx": poll_idx,
        "reference_price": open_px,
        "settle_brti": close,
        "blended_mu": mu,
        "terminal_sigma": sigma,
        "sigma_raw": sigma_raw,
      })
  return pd.DataFrame(rows)


def score_forecast_rows(rows: pd.DataFrame, *, intrahour_only: bool = False) -> dict[str, Any]:
  if rows.empty:
    return {"direction_accuracy": None, "mean_abs_error_usd": None, "n": 0}
  frame = rows if not intrahour_only else rows[rows["poll_idx"] > 0]
  if frame.empty:
    return {"direction_accuracy": None, "mean_abs_error_usd": None, "n": 0}

  correct = [
    _direction_correct(float(r), float(f), float(s))
    for r, f, s in zip(frame["reference_price"], frame["blended_mu"], frame["settle_brti"], strict=False)
  ]
  valid = [c for c in correct if c is not None]
  abs_err = (frame["settle_brti"].astype(float) - frame["blended_mu"].astype(float)).abs()
  return {
    "direction_accuracy": float(np.mean(valid)) if valid else None,
    "mean_abs_error_usd": float(abs_err.mean()),
    "median_abs_error_usd": float(abs_err.median()),
    "n": int(len(frame)),
  }


def fit_sigma_scale(rows: pd.DataFrame, base_scale: float = 1.0) -> float:
  if rows.empty:
    return base_scale
  err = (rows["settle_brti"].astype(float) - rows["blended_mu"].astype(float)).abs()
  sigma = rows["sigma_raw"].astype(float).replace(0, np.nan)
  ratio = float((err / sigma).median())
  if not np.isfinite(ratio) or ratio <= 0:
    return base_scale
  return float(max(0.5, min(2.0, base_scale * ratio)))


@dataclass
class V2CalibrationSearch:
  path_weights: list[float] = field(default_factory=lambda: [0.0, 0.25, 0.40, 0.55])
  momentum_weights: list[float] = field(default_factory=lambda: [0.25, 0.35, 0.45])
  recovery_weights: list[float] = field(default_factory=lambda: [0.15, 0.25, 0.35])
  shock_thresholds: list[float] = field(default_factory=lambda: [0.40, 0.55])


def calibrate_v2_from_backtest(
  cfg: dict[str, Any],
  df_1h: pd.DataFrame,
  *,
  train_frac: float = 0.70,
  warmup_bars: int = 24,
  sample_stride: int = 4,
  search: V2CalibrationSearch | None = None,
  validate_mechanics: bool = True,
  max_train_hours: int = 5000,
) -> dict[str, Any]:
  """Grid-search V2 blend/path weights on train split; fit sigma; validate on holdout."""
  search = search or V2CalibrationSearch()
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  split = int(len(df) * train_frac)
  train_df = df.iloc[:split].reset_index(drop=True)
  holdout_df = df.iloc[split:].reset_index(drop=True)
  search_stride = max(sample_stride, 4)
  intrahour_polls = (1, 2, 3)

  default = V2CalibrationParams().normalized_blend()
  train_hours = _precompute_hours(
    train_df, warmup_bars=warmup_bars, sample_stride=search_stride, max_hours=max_train_hours,
  )
  best_params = default
  best_score = -1.0
  candidates: list[dict[str, Any]] = []

  for pw in search.path_weights:
    for mw in search.momentum_weights:
      for rw in search.recovery_weights:
        for shock in search.shock_thresholds:
          params = V2CalibrationParams(
            path_weight=pw,
            structure_weight=1.0 - pw,
            momentum_weight=mw,
            recovery_weight=rw,
            shock_threshold_pct=shock,
          ).normalized_blend()
          rows = _rows_from_hours(train_hours, params, intrahour_polls)
          metrics = score_forecast_rows(rows, intrahour_only=False)
          acc = metrics.get("direction_accuracy")
          if acc is None:
            continue
          mae = metrics.get("mean_abs_error_usd") or 0.0
          score = acc - mae / 100_000.0
          candidates.append({"params": asdict(params), "train_intrahour": metrics, "score": score})
          if score > best_score:
            best_score = score
            best_params = params

  train_full_hours = _precompute_hours(train_df, warmup_bars=warmup_bars, sample_stride=1)
  holdout_hours = _precompute_hours(holdout_df, warmup_bars=warmup_bars, sample_stride=1)

  train_rows = _rows_from_hours(train_full_hours, best_params, (0, 1, 2, 3))
  sigma_scale = fit_sigma_scale(train_rows)
  best_params.sigma_scale = sigma_scale
  train_rows = _rows_from_hours(train_full_hours, best_params, (0, 1, 2, 3))

  holdout_rows = _rows_from_hours(holdout_hours, best_params, (0, 1, 2, 3))
  default_rows = _rows_from_hours(holdout_hours, default, (0, 1, 2, 3))

  result: dict[str, Any] = {
    "default_params": asdict(default),
    "calibrated_params": asdict(best_params.normalized_blend()),
    "train": {
      "bars": len(train_df),
      "all_polls": score_forecast_rows(train_rows, intrahour_only=False),
      "intrahour_polls": score_forecast_rows(train_rows, intrahour_only=True),
    },
    "holdout": {
      "bars": len(holdout_df),
      "calibrated": {
        "all_polls": score_forecast_rows(holdout_rows, intrahour_only=False),
        "intrahour_polls": score_forecast_rows(holdout_rows, intrahour_only=True),
      },
      "default": {
        "all_polls": score_forecast_rows(default_rows, intrahour_only=False),
        "intrahour_polls": score_forecast_rows(default_rows, intrahour_only=True),
      },
    },
    "search": {
      "n_candidates": len(candidates),
      "best_score": best_score,
      "top_5": sorted(candidates, key=lambda x: x["score"], reverse=True)[:5],
    },
  }

  if validate_mechanics and len(holdout_df) > warmup_bars + 48:
    cal_cfg = _cfg_with_params(cfg, best_params)
    def_cfg = _cfg_with_params(cfg, default)
    result["holdout"]["mechanics_calibrated"] = run_mechanics_backtest(
      holdout_df, cal_cfg, profile="current", mu_mode="v2_path",
    )
    result["holdout"]["mechanics_default"] = run_mechanics_backtest(
      holdout_df, def_cfg, profile="current", mu_mode="v2_path",
    )
    cal_pnl = result["holdout"]["mechanics_calibrated"]["total_pnl_usd"]
    def_pnl = result["holdout"]["mechanics_default"]["total_pnl_usd"]
    result["holdout"]["mechanics_delta_usd"] = round(cal_pnl - def_pnl, 2)

  return result
