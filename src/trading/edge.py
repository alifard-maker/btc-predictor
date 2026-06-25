from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Signal(Enum):
  NO_TRADE = "NO TRADE"
  LONG = "LONG"
  SHORT = "SHORT"


@dataclass
class EdgeResult:
  prob_up: float
  prob_down: float
  net_edge_long: float
  net_edge_short: float
  signal: Signal
  min_confidence_required: float


class EdgeCalculator:
  """Account for fees and slippage when deciding whether to trade."""

  def __init__(self, cfg: dict[str, Any]):
    fees = cfg.get("fees", {})
    self.round_trip_cost = (
      fees.get("taker_pct", 0.10) * 2 + cfg.get("slippage_pct", 0.05) * 2
    ) / 100  # convert pct to fraction
    self.min_confidence = cfg.get("min_edge_confidence", 0.57)
    self.no_trade_band = cfg.get("no_trade_band", 0.03)

  def net_edge(self, prob_up: float) -> tuple[float, float]:
    """Return (long_edge, short_edge) as probability minus break-even."""
    # Break-even: need prob > 0.5 + round_trip_cost (approx) for long
    breakeven_long = 0.5 + self.round_trip_cost / 2
    breakeven_short = 0.5 + self.round_trip_cost / 2

    long_edge = prob_up - breakeven_long
    short_edge = (1 - prob_up) - breakeven_short
    return long_edge, short_edge

  def recommend(self, prob_up: float) -> Signal:
    long_edge, short_edge = self.net_edge(prob_up)

    # Must exceed minimum confidence AND have positive edge
    if prob_up >= self.min_confidence and long_edge > 0:
      return Signal.LONG
    if prob_up <= (1 - self.min_confidence) and short_edge > 0:
      return Signal.SHORT

    # No-trade band around 50%
    if abs(prob_up - 0.5) < self.no_trade_band:
      return Signal.NO_TRADE

    return Signal.NO_TRADE

  def evaluate(self, prob_up: float) -> EdgeResult:
    long_edge, short_edge = self.net_edge(prob_up)
    return EdgeResult(
      prob_up=prob_up,
      prob_down=1 - prob_up,
      net_edge_long=long_edge,
      net_edge_short=short_edge,
      signal=self.recommend(prob_up),
      min_confidence_required=self.min_confidence,
    )
