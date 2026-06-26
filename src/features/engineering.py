from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.features.slots import floor_to_15m


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
  delta = series.diff()
  gain = delta.clip(lower=0).rolling(period).mean()
  loss = (-delta.clip(upper=0)).rolling(period).mean()
  rs = gain / loss.replace(0, np.nan)
  return 100 - (100 / (1 + rs))


def _vwap(df: pd.DataFrame) -> pd.Series:
  typical = (df["high"] + df["low"] + df["close"]) / 3
  cum_vol = df["volume"].cumsum()
  cum_tp_vol = (typical * df["volume"]).cumsum()
  return cum_tp_vol / cum_vol.replace(0, np.nan)


def compute_stage1_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
  """Stage 1: momentum, volatility, wick, volume spike, RSI, VWAP distance."""
  feat_cfg = cfg.get("features", {})
  out = df.copy()

  # Returns & momentum
  out["return_1"] = out["close"].pct_change()
  for w in feat_cfg.get("momentum_windows", [5, 15, 30]):
    out[f"momentum_{w}"] = out["close"].pct_change(w)
    out[f"roc_{w}"] = out["close"].diff(w) / out["close"].shift(w)

  # Volatility
  vol_w = feat_cfg.get("volatility_window", 20)
  out["volatility"] = out["return_1"].rolling(vol_w).std()
  out["volatility_ratio"] = out["volatility"] / out["volatility"].rolling(vol_w * 3).mean()

  # Wick behavior
  body = (out["close"] - out["open"]).abs()
  full_range = (out["high"] - out["low"]).replace(0, np.nan)
  out["upper_wick_ratio"] = (out["high"] - out[["open", "close"]].max(axis=1)) / full_range
  out["lower_wick_ratio"] = (out[["open", "close"]].min(axis=1) - out["low"]) / full_range
  out["body_ratio"] = body / full_range

  # Volume spike
  vol_spike_w = feat_cfg.get("volume_spike_window", 20)
  vol_ma = out["volume"].rolling(vol_spike_w).mean()
  out["volume_spike"] = out["volume"] / vol_ma.replace(0, np.nan)

  # RSI
  rsi_period = feat_cfg.get("rsi_period", 14)
  out["rsi"] = _rsi(out["close"], rsi_period)
  out["rsi_norm"] = (out["rsi"] - 50) / 50

  # VWAP distance
  vwap_w = feat_cfg.get("vwap_window", 48)
  rolling_vwap = (
    (out["close"] * out["volume"]).rolling(vwap_w).sum()
    / out["volume"].rolling(vwap_w).sum()
  )
  out["vwap"] = rolling_vwap
  out["vwap_distance"] = (out["close"] - rolling_vwap) / rolling_vwap.replace(0, np.nan)
  out["vwap_distance_pct"] = out["vwap_distance"] * 100

  return out


def compute_waveform_features(df: pd.DataFrame) -> pd.DataFrame:
  """Phase 2: velocity, acceleration, jerk, curvature, energy, entropy."""
  out = df.copy()
  price = out["close"].astype(float)

  # Derivatives (normalized by price level)
  velocity = price.diff()
  acceleration = velocity.diff()
  jerk = acceleration.diff()

  out["velocity"] = velocity / price
  out["acceleration"] = acceleration / price
  out["jerk"] = jerk / price

  # Curvature: |a| / (1 + v^2)^(3/2) — simplified discrete version
  v = out["velocity"]
  a = out["acceleration"]
  out["curvature"] = a.abs() / (1 + v.pow(2)).pow(1.5)

  # Energy: sum of squared returns over window
  out["energy_10"] = out["return_1"].pow(2).rolling(10).sum() if "return_1" in out else price.pct_change().pow(2).rolling(10).sum()

  # Entropy of return sign distribution
  def _sign_entropy(returns: pd.Series) -> float:
    if returns.isna().all() or len(returns) < 3:
      return np.nan
    signs = np.sign(returns.dropna())
    if len(signs) == 0:
      return np.nan
    p_up = (signs > 0).mean()
    p_down = (signs < 0).mean()
    probs = [p for p in [p_up, p_down] if p > 0]
    return -sum(p * np.log2(p) for p in probs)

  ret = out.get("return_1", price.pct_change())
  out["entropy_20"] = ret.rolling(20).apply(_sign_entropy, raw=False)

  return out


def compute_market_structure_features(df: pd.DataFrame) -> pd.DataFrame:
  """Phase 2: trend strength, HH/LL, compression, vol expansion, liquidity sweeps."""
  out = df.copy()
  high, low, close = out["high"], out["low"], out["close"]

  # Higher highs / lower lows count over window
  w = 20
  out["higher_high"] = (high > high.shift(1)).rolling(w).sum()
  out["lower_low"] = (low < low.shift(1)).rolling(w).sum()
  out["trend_strength"] = (out["higher_high"] - out["lower_low"]) / w

  # Compression ratio: range contraction
  range_ = high - low
  out["compression_ratio"] = range_.rolling(5).mean() / range_.rolling(20).mean().replace(0, np.nan)

  # Volatility expansion
  if "volatility" in out:
    out["vol_expansion"] = out["volatility"] / out["volatility"].rolling(50).mean().replace(0, np.nan)
  else:
    ret = close.pct_change()
    vol = ret.rolling(20).std()
    out["vol_expansion"] = vol / vol.rolling(50).mean().replace(0, np.nan)

  # Volume imbalance (buy vs sell pressure proxy via close position in range)
  clv = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
  out["volume_imbalance"] = (clv * out["volume"]).rolling(10).sum() / out["volume"].rolling(10).sum().replace(0, np.nan)

  # Liquidity sweep proxy: wick beyond recent range then close back inside
  recent_high = high.rolling(10).max().shift(1)
  recent_low = low.rolling(10).min().shift(1)
  sweep_high = (high > recent_high) & (close < recent_high)
  sweep_low = (low < recent_low) & (close > recent_low)
  out["liquidity_sweep"] = (sweep_high.astype(int) - sweep_low.astype(int)).rolling(5).sum()

  return out


def compute_session_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
  """ET calendar + short-term slot autocorrelation."""
  if not cfg.get("features", {}).get("session", True):
    return df
  out = df.copy()
  tz = cfg.get("timezone", "America/New_York")
  if "timestamp" not in out.columns:
    return out
  ts = pd.to_datetime(out["timestamp"], utc=True)
  local = ts.dt.tz_convert(tz)
  hour = local.dt.hour + local.dt.minute / 60.0
  out["hour_sin"] = np.sin(2 * np.pi * hour / 24)
  out["hour_cos"] = np.cos(2 * np.pi * hour / 24)
  out["dow_sin"] = np.sin(2 * np.pi * local.dt.dayofweek / 7)
  out["dow_cos"] = np.cos(2 * np.pi * local.dt.dayofweek / 7)
  out["is_us_session"] = ((hour >= 9.5) & (hour < 16)).astype(float)
  if "return_1" in out.columns:
    out["prev_slot_up"] = (out["return_1"].shift(1) > 0).astype(float)
    out["prev_slot_return"] = out["return_1"].shift(1)
    out["slot_streak"] = (
      (out["return_1"] > 0).astype(int)
      - (out["return_1"] < 0).astype(int)
    ).rolling(4).sum()
  return out


def _merge_asof_feature(
  base: pd.DataFrame,
  aux: pd.DataFrame,
  value_col: str,
  out_col: str,
) -> pd.DataFrame:
  if aux.empty or value_col not in aux.columns:
    base[out_col] = np.nan
    return base
  left = base[["timestamp"]].copy().sort_values("timestamp")
  right = aux[["timestamp", value_col]].dropna().sort_values("timestamp")
  if right.empty:
    base[out_col] = np.nan
    return base
  merged = pd.merge_asof(left, right, on="timestamp", direction="backward")
  base[out_col] = merged[value_col].values
  return base


def merge_auxiliary_features(
  df: pd.DataFrame,
  auxiliary: dict[str, pd.DataFrame] | None,
  cfg: dict,
) -> pd.DataFrame:
  if not cfg.get("features", {}).get("auxiliary", True) or not auxiliary:
    return df
  out = df.copy()
  if "timestamp" not in out.columns:
    return out
  out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
  out = out.sort_values("timestamp")

  funding = auxiliary.get("funding_rate")
  if funding is not None and not funding.empty:
    out = _merge_asof_feature(out, funding, "fundingRate", "funding_rate")
    out["funding_rate_chg"] = out["funding_rate"].diff()

  oi = auxiliary.get("open_interest")
  if oi is not None and not oi.empty:
    col = "sumOpenInterestValue" if "sumOpenInterestValue" in oi.columns else "sumOpenInterest"
    if col in oi.columns:
      out = _merge_asof_feature(out, oi, col, "open_interest")
      out["open_interest_chg"] = out["open_interest"].pct_change()

  nq = auxiliary.get("nasdaq_futures")
  if nq is not None and not nq.empty and "close" in nq.columns:
    nq = nq.copy()
    nq["nq_ret_1"] = nq["close"].pct_change()
    out = _merge_asof_feature(out, nq, "nq_ret_1", "nq_momentum")

  dxy = auxiliary.get("dxy")
  if dxy is not None and not dxy.empty and "close" in dxy.columns:
    dxy = dxy.copy()
    dxy["dxy_ret_1"] = dxy["close"].pct_change()
    out = _merge_asof_feature(out, dxy, "dxy_ret_1", "dxy_momentum")

  return out


def open_drive_features(
  df_1m: pd.DataFrame | None,
  slot_start: pd.Timestamp,
  *,
  tz_name: str = "America/New_York",
  bars: int = 3,
) -> dict[str, float]:
  """Live 1m microstructure at slot open (first N minutes)."""
  if df_1m is None or df_1m.empty:
    return {}
  df = df_1m.copy()
  df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
  slot = pd.Timestamp(slot_start)
  if slot.tzinfo is None:
    slot = slot.tz_localize("UTC")
  in_slot = df[df["timestamp"] >= slot].head(bars)
  if len(in_slot) < 1:
    return {}
  first = float(in_slot.iloc[0]["open"])
  last = float(in_slot.iloc[-1]["close"])
  ret = (last - first) / first if first else 0.0
  vol = float(in_slot["volume"].sum())
  clv = ((in_slot["close"] - in_slot["low"]) - (in_slot["high"] - in_slot["close"])) / (
    (in_slot["high"] - in_slot["low"]).replace(0, np.nan)
  )
  imb = float((clv * in_slot["volume"]).sum() / max(vol, 1e-9))
  return {
    "open_drive_return": ret,
    "open_drive_imbalance": imb,
    "open_drive_bars": float(len(in_slot)),
  }


def inject_row_features(row: pd.Series, extras: dict[str, float]) -> pd.Series:
  out = row.copy()
  for k, v in extras.items():
    out[k] = v
  return out


def _slot_window_label(candles: int) -> str:
  hours = candles * 15 // 60
  return f"{hours}h" if hours * 60 == candles * 15 else f"w{candles}"


def _add_slot_features(out: pd.DataFrame, lookback: int, label: str) -> None:
  sfx = f"_{label}"
  out[f"slot_up_ratio{sfx}"] = (out["return_1"] > 0).rolling(lookback).mean()
  out[f"slot_return{sfx}"] = out["close"].pct_change(lookback)
  out[f"slot_range{sfx}"] = (
    (out["high"].rolling(lookback).max() - out["low"].rolling(lookback).min())
    / out["close"].replace(0, np.nan)
  )
  out[f"slot_volume{sfx}"] = out["volume"].rolling(lookback).sum()
  out[f"slot_volatility{sfx}"] = out["return_1"].rolling(lookback).std()
  out[f"slot_higher_highs{sfx}"] = (out["high"] > out["high"].shift(1)).rolling(lookback).sum()
  out[f"slot_lower_lows{sfx}"] = (out["low"] < out["low"].shift(1)).rolling(lookback).sum()


def compute_slot_context_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
  """Multi-scale slot context: 1h (fast), 4h (primary), 12h (regime)."""
  out = df.copy()
  feat_cfg = cfg.get("features", {})
  windows = feat_cfg.get("slot_context_windows", [4, 16, 48])
  primary = feat_cfg.get("slot_lookback_candles", 16)

  if "return_1" not in out:
    out["return_1"] = out["close"].pct_change()

  seen_labels: set[str] = set()
  for w in windows:
    label = _slot_window_label(int(w))
    if label in seen_labels:
      continue
    seen_labels.add(label)
    _add_slot_features(out, int(w), label)

  primary_label = _slot_window_label(int(primary))
  for stem in (
    "slot_up_ratio",
    "slot_return",
    "slot_range",
    "slot_volume",
    "slot_volatility",
    "slot_higher_highs",
    "slot_lower_lows",
  ):
    src = f"{stem}_{primary_label}"
    if src in out.columns:
      out[stem] = out[src]
  # Legacy alias used in earlier feature sets
  if f"slot_return_{primary_label}" in out.columns:
    out["slot_return_lb"] = out[f"slot_return_{primary_label}"]

  return out


def build_feature_matrix(
  df_primary: pd.DataFrame,
  df_context: pd.DataFrame | None = None,
  cfg: dict | None = None,
  include_phase2: bool = True,
  primary_timeframe: str = "15m",
  auxiliary: dict[str, pd.DataFrame] | None = None,
  open_drive: dict[str, float] | None = None,
) -> pd.DataFrame:
  """
  Build features on primary timeframe (default 15m).
  df_context: optional 1m data merged for microstructure within slots.
  """
  cfg = cfg or {}
  base = compute_stage1_features(df_primary, cfg)

  if include_phase2:
    base = compute_waveform_features(base)
    base = compute_market_structure_features(base)

  base = compute_slot_context_features(base, cfg)
  base = compute_session_features(base, cfg)
  base = merge_auxiliary_features(base, auxiliary, cfg)

  if df_context is not None and not df_context.empty and primary_timeframe == "15m":
    ctx = df_context.copy()
    ctx["timestamp"] = pd.to_datetime(ctx["timestamp"], utc=True)
    tz = cfg.get("timezone", "America/New_York")
    ctx["slot_start"] = ctx["timestamp"].apply(lambda t: floor_to_15m(t, tz))
    agg = ctx.groupby("slot_start").agg(
      m1_return=("close", lambda s: (s.iloc[-1] - s.iloc[0]) / s.iloc[0] if len(s) > 1 else 0),
      m1_volatility=("close", lambda s: s.pct_change().std() if len(s) > 2 else 0),
      m1_volume=("volume", "sum"),
      m1_bars=("close", "count"),
    ).reset_index().rename(columns={"slot_start": "timestamp"})
    base = pd.merge(base, agg, on="timestamp", how="left")

  if open_drive and len(base):
    idx = base.index[-1]
    for key, val in open_drive.items():
      base.loc[idx, key] = val

  return base


def build_feature_matrix_1m_15m(
  df_1m: pd.DataFrame,
  df_15m: pd.DataFrame | None,
  cfg: dict,
  include_phase2: bool = True,
) -> pd.DataFrame:
  """Backward-compatible: 15m primary with 1m context."""
  primary = df_15m if df_15m is not None and not df_15m.empty else df_1m
  return build_feature_matrix(primary, df_1m, cfg, include_phase2, "15m")


def feature_columns(df: pd.DataFrame) -> list[str]:
  """Return model-ready numeric feature column names."""
  exclude = {
    "timestamp", "open", "high", "low", "close", "volume", "vwap", "label",
    "future_return", "future_close",
  }
  return [c for c in df.columns if c not in exclude and pd.api.types.is_numeric_dtype(df[c])]


def add_label(df: pd.DataFrame, horizon_minutes: int = 15, timeframe_minutes: int = 15) -> pd.DataFrame:
  """
  Label: 1 if price is higher after the prediction horizon.
  On 15m candles, horizon=15 means shift(-1) — next candle = next 15m slot.
  """
  out = df.copy()
  candles_ahead = max(1, horizon_minutes // timeframe_minutes)
  out["future_close"] = out["close"].shift(-candles_ahead)
  out["future_return"] = (out["future_close"] - out["close"]) / out["close"]
  out["label"] = (out["future_return"] > 0).astype(int)
  return out
