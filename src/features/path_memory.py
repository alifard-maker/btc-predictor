"""Intrahour path features — nonlinear memory of price trajectory since hour open."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


def _floor_hour_et(ts: pd.Timestamp, tz_name: str = "America/New_York") -> pd.Timestamp:
  ts = pd.Timestamp(ts)
  if ts.tzinfo is None:
    ts = ts.tz_localize("UTC")
  local = ts.tz_convert(tz_name)
  floored = local.replace(minute=0, second=0, microsecond=0)
  return floored.tz_convert("UTC")


def path_memory_from_1m(
  df_1m: pd.DataFrame | None,
  *,
  hour_open: pd.Timestamp | None = None,
  lock_price: float | None = None,
  current_price: float | None = None,
  tz_name: str = "America/New_York",
) -> dict[str, float | None]:
  """Compute path-memory features from 1m candles since the current hour open."""
  empty: dict[str, float | None] = {
    "return_from_hour_open_pct": None,
    "return_from_lock_pct": None,
    "max_runup_pct": None,
    "max_drawdown_pct": None,
    "recovery_ratio": None,
    "path_volatility_pct": None,
    "minutes_elapsed": None,
    "shock_flag": 0.0,
    "momentum_score": None,
  }
  if df_1m is None or df_1m.empty or current_price is None or current_price <= 0:
    return empty

  frame = df_1m.copy()
  if "timestamp" not in frame.columns:
    return empty
  frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
  frame = frame.sort_values("timestamp")
  now = pd.Timestamp.now(tz=timezone.utc)
  hour_start = hour_open if hour_open is not None else _floor_hour_et(now, tz_name)
  if hour_start.tzinfo is None:
    hour_start = hour_start.tz_localize("UTC")

  hour_bars = frame[frame["timestamp"] >= hour_start]
  if hour_bars.empty:
    return empty

  closes = pd.to_numeric(hour_bars["close"], errors="coerce").dropna()
  if closes.empty:
    return empty

  open_px = float(closes.iloc[0])
  if open_px <= 0:
    return empty

  cur = float(current_price)
  rets = closes.pct_change().dropna()
  cum = (closes / open_px - 1.0) * 100.0
  max_runup = float(cum.max())
  max_drawdown = float(cum.min())

  return_from_open = (cur / open_px - 1.0) * 100.0
  return_from_lock = None
  if lock_price is not None and lock_price > 0:
    return_from_lock = (cur / float(lock_price) - 1.0) * 100.0

  trough = float(closes.min())
  recovery_ratio = None
  if max_drawdown < -0.05 and trough < cur:
    denom = open_px - trough
    if denom > 0:
      recovery_ratio = float((cur - trough) / denom)

  path_vol = float(rets.std() * 100.0) if len(rets) >= 3 else None
  minutes_elapsed = float((now - hour_start).total_seconds() / 60.0)
  shock_flag = 1.0 if abs(return_from_open) >= 0.5 else 0.0

  momentum_score = float(np.tanh(return_from_open / 0.35))

  return {
    "return_from_hour_open_pct": return_from_open,
    "return_from_lock_pct": return_from_lock,
    "max_runup_pct": max_runup,
    "max_drawdown_pct": max_drawdown,
    "recovery_ratio": recovery_ratio,
    "path_volatility_pct": path_vol,
    "minutes_elapsed": minutes_elapsed,
    "shock_flag": shock_flag,
    "momentum_score": momentum_score,
  }


def apply_path_memory_adjustment(
  structure_mu: float,
  structure_sigma: float,
  path: dict[str, float | None],
  hours_left: float,
  *,
  cfg: dict[str, Any] | None = None,
) -> tuple[float, dict[str, float]]:
  """Nonlinear μ shift from intrahour path (independent of v1 ML)."""
  pcfg = (cfg or {}).get("path_memory", {})
  momentum_w = float(pcfg.get("momentum_weight", 0.35))
  recovery_w = float(pcfg.get("recovery_weight", 0.25))
  shock_thresh = float(pcfg.get("shock_threshold_pct", 0.5))

  ret_open = float(path.get("return_from_hour_open_pct") or 0.0)
  recovery = path.get("recovery_ratio")
  max_dd = float(path.get("max_drawdown_pct") or 0.0)
  time_weight = max(0.0, min(1.0, 1.0 - max(0.0, hours_left)))

  momentum_term = np.tanh(ret_open / max(shock_thresh, 0.1)) * structure_sigma * momentum_w
  recovery_term = 0.0
  if recovery is not None and max_dd < -shock_thresh:
    recovery_term = (float(recovery) - 0.5) * structure_sigma * recovery_w

  shift = float(momentum_term * (0.45 + 0.55 * time_weight) + recovery_term)
  adjusted = structure_mu + shift
  detail = {
    "momentum_term": float(momentum_term),
    "recovery_term": float(recovery_term),
    "path_shift_usd": shift,
    "time_weight": time_weight,
  }
  return adjusted, detail
