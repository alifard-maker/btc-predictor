"""Support / resistance and consolidation detection from OHLC candles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PriceLevel:
  price: float
  level_type: str  # support | resistance
  touches: int
  wick_score: float  # 0-1 rejection strength at level
  volume_confirmed: bool
  strength: float  # combined 0-1


@dataclass(frozen=True)
class ConsolidationBox:
  low: float
  high: float
  hours: float
  tightness: float  # lower = tighter range vs recent vol


def _prep_df(df: pd.DataFrame) -> pd.DataFrame:
  out = df.copy()
  out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
  return out.sort_values("timestamp")


def _cluster_levels(prices: list[float], tolerance_pct: float = 0.08) -> list[tuple[float, int]]:
  """Cluster nearby prices into levels; return (center, touch_count)."""
  if not prices:
    return []
  prices = sorted(prices)
  clusters: list[list[float]] = [[prices[0]]]
  for p in prices[1:]:
    center = float(np.mean(clusters[-1]))
    if center > 0 and abs(p - center) / center * 100 <= tolerance_pct:
      clusters[-1].append(p)
    else:
      clusters.append([p])
  return [(float(np.mean(c)), len(c)) for c in clusters if len(c) >= 1]


def detect_levels(
  df: pd.DataFrame,
  current_price: float,
  *,
  touch_tolerance_pct: float = 0.12,
  min_touches: int = 2,
  wick_ratio_min: float = 0.35,
) -> list[PriceLevel]:
  """Multi-touch support/resistance with wick and volume clues."""
  if df is None or df.empty or current_price <= 0:
    return []

  df = _prep_df(df)
  if len(df) < 8:
    return []

  highs = df["high"].astype(float)
  lows = df["low"].astype(float)
  opens = df["open"].astype(float)
  closes = df["close"].astype(float)
  vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series(1.0, index=df.index)
  vol_ma = float(vol.rolling(min(24, len(vol)), min_periods=4).mean().iloc[-1] or vol.mean())

  support_candidates: list[float] = []
  resistance_candidates: list[float] = []
  support_wicks: dict[float, float] = {}
  resistance_wicks: dict[float, float] = {}
  support_vol: dict[float, bool] = {}
  resistance_vol: dict[float, bool] = {}

  for i in range(len(df)):
    h, l, o, c = float(highs.iloc[i]), float(lows.iloc[i]), float(opens.iloc[i]), float(closes.iloc[i])
    body = max(abs(c - o), 1e-9)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    bar_vol = float(vol.iloc[i])
    vol_ok = bar_vol >= vol_ma * 0.85 if vol_ma > 0 else True

    # Bounce off support: long lower wick, close off the low
    if lower_wick / (h - l + 1e-9) >= wick_ratio_min and c > l + lower_wick * 0.4:
      support_candidates.append(l)
      key = round(l, -1)
      support_wicks[key] = max(support_wicks.get(key, 0), min(1.0, lower_wick / body))
      support_vol[key] = support_vol.get(key, False) or vol_ok

    # Rejection at resistance: long upper wick
    if upper_wick / (h - l + 1e-9) >= wick_ratio_min and c < h - upper_wick * 0.4:
      resistance_candidates.append(h)
      key = round(h, -1)
      resistance_wicks[key] = max(resistance_wicks.get(key, 0), min(1.0, upper_wick / body))
      resistance_vol[key] = resistance_vol.get(key, False) or vol_ok

    # Plain touch of rolling window extremes
    if i >= 3:
      window_low = float(lows.iloc[max(0, i - 3): i + 1].min())
      window_high = float(highs.iloc[max(0, i - 3): i + 1].max())
      if abs(l - window_low) / window_low * 100 < touch_tolerance_pct:
        support_candidates.append(l)
      if abs(h - window_high) / window_high * 100 < touch_tolerance_pct:
        resistance_candidates.append(h)

  levels: list[PriceLevel] = []
  for center, touches in _cluster_levels(support_candidates, touch_tolerance_pct):
    if touches < min_touches:
      continue
    key = round(center, -1)
    wick = support_wicks.get(key, 0.4)
    vol_ok = support_vol.get(key, False)
    strength = min(1.0, 0.35 * touches + 0.35 * wick + (0.3 if vol_ok else 0))
    levels.append(PriceLevel(center, "support", touches, wick, vol_ok, strength))

  for center, touches in _cluster_levels(resistance_candidates, touch_tolerance_pct):
    if touches < min_touches:
      continue
    key = round(center, -1)
    wick = resistance_wicks.get(key, 0.4)
    vol_ok = resistance_vol.get(key, False)
    strength = min(1.0, 0.35 * touches + 0.35 * wick + (0.3 if vol_ok else 0))
    levels.append(PriceLevel(center, "resistance", touches, wick, vol_ok, strength))

  levels.sort(key=lambda x: abs(x.price - current_price))
  return levels[:12]


def consolidation_box(df: pd.DataFrame, *, lookback_bars: int = 12) -> ConsolidationBox | None:
  """Tight range on recent 1h bars — where price may stall."""
  if df is None or df.empty:
    return None
  df = _prep_df(df).tail(max(6, lookback_bars))
  if len(df) < 6:
    return None

  highs = df["high"].astype(float)
  lows = df["low"].astype(float)
  closes = df["close"].astype(float)
  box_high = float(highs.max())
  box_low = float(lows.min())
  if box_low <= 0:
    return None

  width_pct = (box_high - box_low) / box_low * 100
  rets = closes.pct_change().dropna()
  vol_pct = float(rets.std() * 100) if len(rets) else 0.15
  hours = len(df)  # 1h bars ≈ hours

  return ConsolidationBox(
    low=box_low,
    high=box_high,
    hours=hours,
    tightness=width_pct / max(vol_pct * 4, 0.05),
  )


def levels_to_dict(levels: list[PriceLevel]) -> list[dict[str, Any]]:
  return [
    {
      "price": round(l.price, 2),
      "type": l.level_type,
      "touches": l.touches,
      "wick_score": round(l.wick_score, 2),
      "volume_confirmed": l.volume_confirmed,
      "strength": round(l.strength, 2),
    }
    for l in levels
  ]
