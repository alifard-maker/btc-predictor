"""Read-only forecast-vs-settle stats for locked hourly predictions (independent of bot)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _num(series: pd.Series) -> pd.Series:
  return pd.to_numeric(series, errors="coerce")


def _direction_correct(ref: float, forecast: float, settle: float) -> bool | None:
  if not np.isfinite(ref) or not np.isfinite(forecast) or not np.isfinite(settle):
    return None
  if ref == settle:
    return forecast == ref
  return (settle > ref) == (forecast > ref)


def forecast_vs_settle_frame(df: pd.DataFrame) -> pd.DataFrame:
  """Enrich resolved hourly rows with μ forecast error metrics."""
  if df.empty:
    return df
  out = df.copy()
  ref = _num(out["reference_price"])
  mu = _num(out["blended_mu"])
  settle = _num(out["settle_brti"])
  sigma = _num(out["terminal_sigma"]).replace(0, np.nan)

  out["mu_error_usd"] = settle - mu
  out["mu_abs_error_usd"] = out["mu_error_usd"].abs()
  out["mu_error_pct"] = out["mu_error_usd"] / ref.replace(0, np.nan) * 100
  out["mu_error_sigma"] = out["mu_abs_error_usd"] / sigma

  dir_ok = [
    _direction_correct(float(r), float(f), float(s))
    for r, f, s in zip(ref, mu, settle, strict=False)
  ]
  out["direction_correct"] = dir_ok

  if "settlement_zone_low" in out.columns and "settlement_zone_high" in out.columns:
    zlo = _num(out["settlement_zone_low"])
    zhi = _num(out["settlement_zone_high"])
    has_zone = zlo.notna() & zhi.notna()
    in_zone = pd.Series([None] * len(out), dtype=object)
    in_zone.loc[has_zone] = (
      (settle.loc[has_zone] >= zlo.loc[has_zone])
      & (settle.loc[has_zone] <= zhi.loc[has_zone])
    )
    out["settle_in_zone"] = in_zone

  return out


def _rolling_windows(df: pd.DataFrame, correct: pd.Series, windows: list[int]) -> dict[str, dict[str, Any]]:
  empty = {f"{h}h": {"correct": 0, "total": 0, "accuracy": None} for h in windows}
  if df.empty:
    return empty
  now = pd.Timestamp.now(tz="UTC")
  ts = pd.to_datetime(df["logged_at"], utc=True)
  out: dict[str, dict[str, Any]] = {}
  for h in windows:
    mask = ts >= (now - pd.Timedelta(hours=h))
    sub = correct[mask & correct.notna()]
    n = int(sub.shape[0])
    if n == 0:
      out[f"{h}h"] = {"correct": 0, "total": 0, "accuracy": None}
    else:
      c = int(sub.sum())
      out[f"{h}h"] = {"correct": c, "total": n, "accuracy": float(c / n)}
  return out


def forecast_vs_settle_summary(df: pd.DataFrame, *, windows: list[int] | None = None) -> dict[str, Any]:
  """Aggregate μ forecast quality from resolved :05 lock rows."""
  windows = windows or [1, 2, 4, 12, 24, 168]
  if df.empty or "settle_brti" not in df.columns:
    return {
      "n_resolved": 0,
      "direction_accuracy": None,
      "rolling_direction_accuracy": _rolling_windows(df, pd.Series(dtype=object), windows),
      "mean_abs_error_usd": None,
      "median_abs_error_usd": None,
      "mean_error_sigma": None,
      "in_zone_rate": None,
      "recent": [],
    }

  scored = forecast_vs_settle_frame(df)
  scored = scored[scored["settle_brti"].notna()].copy()
  if scored.empty:
    return {
      "n_resolved": 0,
      "direction_accuracy": None,
      "rolling_direction_accuracy": _rolling_windows(df, pd.Series(dtype=object), windows),
      "mean_abs_error_usd": None,
      "median_abs_error_usd": None,
      "mean_error_sigma": None,
      "in_zone_rate": None,
      "recent": [],
    }

  dir_series = scored["direction_correct"]
  valid_dir = dir_series.notna()
  direction_accuracy = float(dir_series[valid_dir].mean()) if valid_dir.any() else None

  abs_err = scored["mu_abs_error_usd"].dropna()
  err_sigma = scored["mu_error_sigma"].dropna()

  in_zone_rate = None
  if "settle_in_zone" in scored.columns:
    zone = scored["settle_in_zone"].dropna()
    if len(zone):
      in_zone_rate = float(zone.astype(bool).mean())

  recent_rows: list[dict[str, Any]] = []
  tail = scored.sort_values("logged_at", ascending=False).head(12)
  for _, r in tail.iterrows():
    recent_rows.append({
      "logged_at": r.get("logged_at"),
      "event_ticker": r.get("event_ticker"),
      "reference_price": r.get("reference_price"),
      "blended_mu": r.get("blended_mu"),
      "settle_brti": r.get("settle_brti"),
      "mu_error_usd": round(float(r["mu_error_usd"]), 2) if pd.notna(r.get("mu_error_usd")) else None,
      "direction_correct": r.get("direction_correct"),
      "settle_in_zone": r.get("settle_in_zone"),
    })

  return {
    "n_resolved": int(len(scored)),
    "direction_accuracy": direction_accuracy,
    "rolling_direction_accuracy": _rolling_windows(scored, dir_series, windows),
    "mean_abs_error_usd": float(abs_err.mean()) if len(abs_err) else None,
    "median_abs_error_usd": float(abs_err.median()) if len(abs_err) else None,
    "mean_error_sigma": float(err_sigma.mean()) if len(err_sigma) else None,
    "in_zone_rate": in_zone_rate,
    "recent": recent_rows,
  }
