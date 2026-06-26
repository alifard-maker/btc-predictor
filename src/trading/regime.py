"""Regime gates — skip low-edge chop and fee-dominated setups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.trading.edge import Signal


@dataclass(frozen=True)
class RegimeDecision:
  allow_trade: bool
  reasons: list[str]


class RegimeFilter:
  def __init__(self, cfg: dict[str, Any]):
    rcfg = cfg.get("regime", {})
    self.enabled = bool(rcfg.get("enabled", True))
    fees = cfg.get("fees", {})
    slip = cfg.get("slippage_pct", 0.05)
    round_trip_pct = fees.get("taker_pct", 0.10) * 2 + slip * 2
    self.min_expected_move_pct = float(
      rcfg.get("min_expected_move_pct", cfg.get("intra_slot", {}).get("fee_buffer_pct", round_trip_pct / 2))
    )
    self.max_compression = float(rcfg.get("max_compression_ratio", 1.12))
    self.min_vol_expansion = float(rcfg.get("min_vol_expansion", 0.82))
    self.block_low_entropy_trend = bool(rcfg.get("block_low_entropy_trend", False))

  def evaluate(self, row: pd.Series, *, expected_move_pct: float) -> RegimeDecision:
    if not self.enabled:
      return RegimeDecision(True, [])

    reasons: list[str] = []

    compression = row.get("compression_ratio")
    if compression is not None and not pd.isna(compression) and float(compression) > self.max_compression:
      reasons.append(f"Range compressed ({float(compression):.2f}×) — chop risk")

    vol_exp = row.get("vol_expansion")
    if vol_exp is not None and not pd.isna(vol_exp) and float(vol_exp) < self.min_vol_expansion:
      reasons.append(f"Vol not expanding ({float(vol_exp):.2f}×)")

    if abs(expected_move_pct) < self.min_expected_move_pct:
      reasons.append(
        f"Expected move {expected_move_pct:.3f}% below {self.min_expected_move_pct:.2f}% fee floor"
      )

    entropy = row.get("entropy_20")
    trend = row.get("trend_strength")
    if (
      self.block_low_entropy_trend
      and entropy is not None
      and not pd.isna(entropy)
      and float(entropy) < 0.85
      and trend is not None
      and not pd.isna(trend)
      and abs(float(trend)) < 0.15
    ):
      reasons.append("Low directional entropy — no clear trend")

    return RegimeDecision(allow_trade=len(reasons) == 0, reasons=reasons)

  def gate_signal(self, signal: Signal, decision: RegimeDecision) -> Signal:
    if signal == Signal.NO_TRADE or decision.allow_trade:
      return signal
    return Signal.NO_TRADE
