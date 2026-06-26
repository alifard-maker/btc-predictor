"""Hourly boundary labels for 1h model training."""

from __future__ import annotations

import pandas as pd


def floor_to_hour(ts: pd.Timestamp, tz_name: str = "America/New_York") -> pd.Timestamp:
  ts = pd.Timestamp(ts)
  if ts.tzinfo is None:
    ts = ts.tz_localize("UTC")
  local = ts.tz_convert(tz_name)
  floored = local.replace(minute=0, second=0, microsecond=0)
  return floored.tz_convert("UTC")


def add_hourly_label(
  df: pd.DataFrame,
  *,
  tz_name: str = "America/New_York",
) -> pd.DataFrame:
  """Label: 1 if next hour close > current hour close."""
  out = df.copy()
  out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
  out["_hour"] = out["timestamp"].apply(lambda t: floor_to_hour(t, tz_name))
  out = out.sort_values("timestamp").groupby("_hour", as_index=False).last()
  out["timestamp"] = out["_hour"]
  out["future_close"] = out["close"].shift(-1)
  out["future_return"] = (out["future_close"] - out["close"]) / out["close"]
  out["label"] = (out["future_return"] > 0).astype(int)
  return out.drop(columns=["_hour"], errors="ignore")
