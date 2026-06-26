"""Regime gates for hourly Kalshi threshold picks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class HourlyRegimeDecision:
  allow_trade: bool
  reasons: list[str]


class HourlyRegimeFilter:
  def __init__(self, cfg: dict[str, Any]):
    hcfg = cfg.get("hourly", {}).get("regime", {})
    self.enabled = bool(hcfg.get("enabled", True))
    self.min_expected_move_pct = float(hcfg.get("min_expected_move_pct", 0.12))
    self.min_edge = float(hcfg.get("min_edge", cfg.get("hourly", {}).get("min_edge", 0.05)))
    self.min_hours_to_settle = float(hcfg.get("min_hours_to_settle", 0.25))
    self.max_sigma_pct = float(hcfg.get("max_sigma_pct", 2.5))
    self.min_reasons_to_block = int(hcfg.get("min_reasons_to_block", 2))

  def evaluate(
    self,
    *,
    expected_move_pct: float,
    hours_to_settle: float,
    sigma_pct: float,
    edge: float | None,
    compression: float | None = None,
  ) -> HourlyRegimeDecision:
    if not self.enabled:
      return HourlyRegimeDecision(True, [])

    reasons: list[str] = []
    if abs(expected_move_pct) < self.min_expected_move_pct:
      reasons.append(
        f"Expected move {expected_move_pct:.2f}% below {self.min_expected_move_pct:.2f}% floor"
      )
    if hours_to_settle < self.min_hours_to_settle:
      reasons.append(f"Only {hours_to_settle:.2f}h to settle — too late for new lean")
    if sigma_pct > self.max_sigma_pct:
      reasons.append(f"Terminal σ {sigma_pct:.2f}% — wide uncertainty")
    if edge is not None and abs(edge) < self.min_edge:
      reasons.append(f"Edge {edge * 100:.1f}¢ below minimum")
    if compression is not None and not pd.isna(compression) and float(compression) > 1.15:
      reasons.append(f"Range compressed ({float(compression):.2f}×)")

    block = len(reasons) >= self.min_reasons_to_block
    return HourlyRegimeDecision(allow_trade=not block, reasons=reasons)
