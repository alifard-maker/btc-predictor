"""Structure-aware μ/σ adjustment for backtests (consolidation + S/R memory)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.features.levels import consolidation_box, detect_levels


@dataclass(frozen=True)
class StructureMemoryConfig:
  lookback_bars: int = 12
  """1h bars of history for levels/consolidation (~hours of memory)."""
  mu_pull_strength: float = 0.35
  resistance_mu_penalty: float = 0.4
  sigma_inflate_tight: float = 1.25
  block_yes_above_in_upper_box: bool = True
  upper_box_fraction: float = 0.7


def adjust_mu_sigma_from_structure(
  mu: float,
  sigma: float,
  current_price: float,
  df_1h: pd.DataFrame | None,
  *,
  cfg: StructureMemoryConfig | None = None,
) -> tuple[float, float, dict[str, Any]]:
  """
  Nonlinear memory: pull μ toward consolidation mid when range-bound; penalize
  breakout μ near overhead resistance; widen σ in tight boxes.
  """
  cfg = cfg or StructureMemoryConfig()
  detail: dict[str, Any] = {"applied": False}
  if df_1h is None or df_1h.empty or current_price <= 0:
    return mu, sigma, detail

  hist = df_1h.sort_values("timestamp").tail(max(6, cfg.lookback_bars))
  if len(hist) < 6:
    return mu, sigma, detail

  box = consolidation_box(hist, lookback_bars=cfg.lookback_bars)
  levels = detect_levels(hist, current_price)
  if box is None:
    return mu, sigma, detail

  detail["applied"] = True
  detail["box_low"] = box.low
  detail["box_high"] = box.high
  detail["tightness"] = round(box.tightness, 3)
  detail["lookback_bars"] = cfg.lookback_bars

  adjusted_mu = float(mu)
  adjusted_sigma = float(sigma)
  span = max(1.0, box.high - box.low)
  pos_in_box = (current_price - box.low) / span

  if box.tightness < 2.0:
    box_mid = (box.high + box.low) / 2.0
    pull = cfg.mu_pull_strength * (1.0 - min(1.0, box.tightness / 2.0))
    adjusted_mu = adjusted_mu * (1.0 - pull) + box_mid * pull
    if box.tightness < 1.5:
      adjusted_sigma *= cfg.sigma_inflate_tight
    detail["mu_pull_to_box_mid"] = round(pull, 3)

  resists = [lv for lv in levels if lv.level_type == "resistance" and lv.price >= current_price]
  if resists:
    nearest = min(resists, key=lambda lv: lv.price - current_price)
    dist_pct = (nearest.price - current_price) / current_price * 100.0
    if dist_pct < 0.35:
      pen = cfg.resistance_mu_penalty * nearest.strength * max(0.2, 1.0 - dist_pct / 0.35)
      adjusted_mu -= (adjusted_mu - current_price) * pen
      detail["resistance_penalty"] = round(pen, 3)
      detail["nearest_resistance"] = nearest.price

  detail["pos_in_box"] = round(pos_in_box, 3)
  detail["block_yes_above_breakout"] = (
    cfg.block_yes_above_in_upper_box and box.tightness < 2.0 and pos_in_box >= cfg.upper_box_fraction
  )
  return adjusted_mu, adjusted_sigma, detail


def structure_blocks_yes_above(detail: dict[str, Any]) -> bool:
  return bool(detail.get("block_yes_above_breakout"))
