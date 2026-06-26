"""Slot-aligned labels for 15m ET horizon training."""

from __future__ import annotations

import pandas as pd

from src.features.slots import floor_to_15m


def add_slot_label(
  df: pd.DataFrame,
  *,
  tz_name: str = "America/New_York",
  horizon_minutes: int = 15,
) -> pd.DataFrame:
  """
  Label rows at ET slot boundaries: 1 if next slot close > current close.
  Drops non-boundary rows so training matches live prediction cadence.
  """
  out = df.copy()
  if "timestamp" not in out.columns:
    raise ValueError("features need timestamp column for slot labels")

  out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
  out["_slot"] = out["timestamp"].apply(lambda t: floor_to_15m(t, tz_name))

  # One row per ET slot — exchange candles may be open- or close-timestamped
  out = (
    out.sort_values("timestamp")
    .groupby("_slot", as_index=False)
    .last()
  )
  out["timestamp"] = out["_slot"]

  candles_ahead = max(1, horizon_minutes // 15)
  out["future_close"] = out["close"].shift(-candles_ahead)
  out["future_return"] = (out["future_close"] - out["close"]) / out["close"]
  out["label"] = (out["future_return"] > 0).astype(int)
  return out.drop(columns=["_slot"], errors="ignore")
