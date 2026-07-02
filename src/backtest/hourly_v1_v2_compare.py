"""Compare hourly V1 (ML walk-forward / momentum) vs V2 path memory on historical 1h data."""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.hourly_mechanics_backtest import (
  MuMode,
  _blended_mu_for_poll,
  _brti_poll_prices,
  run_mechanics_backtest,
)
from src.backtest.metrics import compute_metrics
from src.backtest.walk_forward import WalkForwardBacktest, WalkForwardConfig
from src.backtest.fill_simulator import OrderStyle
from src.calibration.forecast_vs_settle import forecast_vs_settle_summary
from src.calibration.v2_calibration import load_v2_calibration
from src.trading.edge import EdgeCalculator, Signal


def _v2_cfg(cfg: dict[str, Any], *, asset: str = "btc") -> dict[str, Any]:
  hcfg = dict(cfg.get("hourly_v2") or {})
  if not hcfg.get("calibration", {}).get("enabled", True):
    return cfg
  cal = load_v2_calibration(cfg, asset=asset)
  if cal is None:
    return cfg
  out = dict(cfg)
  out["hourly_v2"] = cal.apply_to_hcfg(hcfg)
  return out


def _momentum_4h(opens: np.ndarray, idx: int) -> float:
  mom_idx = max(0, idx - 4)
  if opens[mom_idx] <= 0:
    return 0.0
  return (float(opens[idx]) - float(opens[mom_idx])) / float(opens[mom_idx]) * 100.0


def _sigma_from_bar(open_px: float, high: float, low: float) -> float:
  return max(30.0, (high - low) * 0.6 + open_px * 0.001)


def _prob_from_mu(ref: float, mu: float, sigma: float) -> float:
  if ref <= 0 or sigma <= 0:
    return 0.5
  return 0.5 + 0.5 * math.tanh((mu - ref) / sigma)


def run_v1_walk_forward(
  cfg: dict[str, Any],
  df_1h: pd.DataFrame,
  df_15m: pd.DataFrame | None,
  *,
  model_type: str | None = None,
) -> dict[str, Any]:
  """V1 baseline: rolling ML walk-forward with passive fills (yesterday's test)."""
  run_cfg = dict(cfg)
  if model_type:
    run_cfg.setdefault("model", {})["type"] = model_type
  wf = WalkForwardBacktest(run_cfg)
  trade_df, metrics, folds = wf.run(df_1h, df_15m if df_15m is not None and not df_15m.empty else None)
  return {
    "method": "v1_ml_walk_forward",
    "model_type": run_cfg.get("model", {}).get("type", "lightgbm"),
    "metrics": asdict(metrics),
    "n_folds": len(folds),
    "n_trades": int(len(trade_df)),
  }


def run_v2_signal_backtest(
  cfg: dict[str, Any],
  df_1h: pd.DataFrame,
  *,
  warmup_bars: int = 24,
) -> dict[str, Any]:
  """V2 path μ → edge → passive fills (same plumbing as walk-forward, no ML training)."""
  from src.backtest.fee_model import FeeModel
  from src.backtest.fill_simulator import FillSimulator

  cfg = _v2_cfg(cfg)
  edge = EdgeCalculator(cfg)
  fees = FeeModel(cfg=cfg)
  fills = FillSimulator(app_cfg=cfg, fee_model=fees)
  wf_cfg = WalkForwardConfig.from_config(cfg)
  fills._rng = np.random.default_rng(wf_cfg.rng_seed)

  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  opens = df["open"].astype(float).values
  highs = df["high"].astype(float).values
  lows = df["low"].astype(float).values
  closes = df["close"].astype(float).values
  ts = pd.to_datetime(df["timestamp"], utc=True)

  trades: list[dict[str, Any]] = []
  for i in range(warmup_bars, len(df)):
    open_px = float(opens[i])
    high = float(highs[i])
    low = float(lows[i])
    close = float(closes[i])
    mom = _momentum_4h(opens, i)
    sigma = _sigma_from_bar(open_px, high, low)
    polls = _brti_poll_prices(open_px, high, low, close)
    hour_ts = ts.iloc[i].to_pydatetime()
    mu = _blended_mu_for_poll(
      mu_mode="v2_path",
      open_px=open_px,
      high=high,
      low=low,
      close=close,
      hour_open=open_px,
      momentum_4h_pct=mom,
      poll_idx=0,
      poll_prices=polls,
      brti=polls[0],
      hour_ts=hour_ts,
      sigma=sigma,
      cfg=cfg,
    )
    prob_up = _prob_from_mu(open_px, mu, sigma)
    signal = edge.recommend(prob_up)
    actual_up = int(close > open_px)
    if signal == Signal.NO_TRADE:
      trades.append({
        "timestamp": ts.iloc[i],
        "prob_up": prob_up,
        "blended_mu": mu,
        "signal": signal.value,
        "actual_up": actual_up,
        "filled": False,
        "pnl_usd": 0.0,
      })
      continue

    side = "yes" if signal == Signal.LONG else "no"
    fill = fills.simulate_entry(
      prob_up=prob_up,
      side=side,
      order_style=wf_cfg.order_style,
      time_to_settle_hours=wf_cfg.time_to_settle_hours,
      volume_proxy=wf_cfg.volume_proxy,
    )
    won = (side == "yes" and actual_up == 1) or (side == "no" and actual_up == 0)
    pnl_usd = 0.0
    if fill.filled and fill.price_cents is not None:
      pnl_usd = fees.settlement_pnl_usd(
        side=side,
        entry_price_cents=fill.price_cents,
        contracts=fill.contracts,
        won=won,
        entry_maker=fill.is_maker,
      )
    trades.append({
      "timestamp": ts.iloc[i],
      "prob_up": prob_up,
      "blended_mu": mu,
      "signal": signal.value,
      "side": side,
      "actual_up": actual_up,
      "won": won,
      "filled": fill.filled,
      "pnl_usd": pnl_usd,
    })

  trade_df = pd.DataFrame(trades)
  metrics = compute_metrics(
    trade_df,
    n_bootstrap=wf_cfg.bootstrap_samples,
    alpha=wf_cfg.bootstrap_alpha,
  )
  return {
    "method": "v2_path_signal",
    "metrics": asdict(metrics),
    "n_hours": len(trades),
  }


def run_forecast_comparison(
  cfg: dict[str, Any],
  df_1h: pd.DataFrame,
  *,
  warmup_bars: int = 24,
) -> dict[str, Any]:
  """Direction + μ error at hour lock (ref=open, settle=close) for momentum vs v2."""
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  opens = df["open"].astype(float).values
  highs = df["high"].astype(float).values
  lows = df["low"].astype(float).values
  closes = df["close"].astype(float).values
  ts = pd.to_datetime(df["timestamp"], utc=True)

  rows: list[dict[str, Any]] = []
  for i in range(warmup_bars, len(df)):
    open_px = float(opens[i])
    high = float(highs[i])
    low = float(lows[i])
    close = float(closes[i])
    mom = _momentum_4h(opens, i)
    sigma = _sigma_from_bar(open_px, high, low)
    polls = _brti_poll_prices(open_px, high, low, close)
    hour_ts = ts.iloc[i].to_pydatetime()

    v1_mu = open_px * (1.0 + mom / 100.0 * 0.25)
    v2_mu = _blended_mu_for_poll(
      mu_mode="v2_path",
      open_px=open_px,
      high=high,
      low=low,
      close=close,
      hour_open=open_px,
      momentum_4h_pct=mom,
      poll_idx=0,
      poll_prices=polls,
      brti=polls[0],
      hour_ts=hour_ts,
      sigma=sigma,
      cfg=cfg,
    )
    rows.append({
      "logged_at": ts.iloc[i],
      "reference_price": open_px,
      "settle_brti": close,
      "terminal_sigma": sigma,
      "v1_momentum_mu": v1_mu,
      "v2_path_mu": v2_mu,
    })

  frame = pd.DataFrame(rows)
  out: dict[str, Any] = {"n_hours": len(frame)}
  for label, col in (("v1_momentum", "v1_momentum_mu"), ("v2_path_lock", "v2_path_mu")):
    sub = frame.copy()
    sub["blended_mu"] = sub[col]
    out[label] = forecast_vs_settle_summary(sub, windows=[])
  return out


def run_mechanics_mu_compare(
  cfg: dict[str, Any],
  df_1h: pd.DataFrame,
  *,
  profile: str = "current",
  max_spend: float = 15.0,
  warmup_bars: int = 24,
) -> dict[str, Any]:
  """Same bot mechanics (current deploy profile), momentum μ vs v2 path μ."""
  v2_cfg = _v2_cfg(cfg)
  results: dict[str, Any] = {}
  for mode in ("momentum", "v2_path"):
    results[mode] = run_mechanics_backtest(
      df_1h,
      v2_cfg if mode == "v2_path" else cfg,
      profile=profile,  # type: ignore[arg-type]
      max_spend=max_spend,
      warmup_bars=warmup_bars,
      mu_mode=mode,  # type: ignore[arg-type]
    )
  mom = results["momentum"]
  v2 = results["v2_path"]
  return {
    "profile": profile,
    "v1_momentum": mom,
    "v2_path": v2,
    "v2_minus_v1_pnl_usd": round(v2["total_pnl_usd"] - mom["total_pnl_usd"], 2),
    "v2_minus_v1_expectancy_per_fill": round(
      (v2.get("expectancy_per_fill_usd") or 0) - (mom.get("expectancy_per_fill_usd") or 0),
      4,
    ),
  }


def run_full_comparison(
  cfg: dict[str, Any],
  df_1h: pd.DataFrame,
  df_15m: pd.DataFrame | None = None,
  *,
  years: float = 3.0,
  model_type: str | None = "random_forest",
  include_walk_forward: bool = True,
) -> dict[str, Any]:
  df = df_1h.sort_values("timestamp").reset_index(drop=True)
  if years > 0:
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=int(years * 365.25))
    df = df[df["timestamp"] >= start].reset_index(drop=True)

  payload: dict[str, Any] = {
    "bars": len(df),
    "period_start": str(df["timestamp"].min()) if len(df) else None,
    "period_end": str(df["timestamp"].max()) if len(df) else None,
    "disclaimer": (
      "Synthetic Kalshi books + 1h OHLC. V1 ML walk-forward uses rolling train/test; "
      "V2 path uses intrahour synthetic polls. Not historical Kalshi contract prices."
    ),
  }

  payload["forecast"] = run_forecast_comparison(cfg, df)
  payload["mechanics"] = run_mechanics_mu_compare(cfg, df)
  payload["v2_signal"] = run_v2_signal_backtest(cfg, df)

  if include_walk_forward:
    ctx = df_15m
    if ctx is not None and years > 0 and len(ctx):
      end = df["timestamp"].max()
      start = df["timestamp"].min()
      ctx = ctx[(ctx["timestamp"] >= start) & (ctx["timestamp"] <= end)].reset_index(drop=True)
    payload["v1_walk_forward"] = run_v1_walk_forward(cfg, df, ctx, model_type=model_type)

  return payload
