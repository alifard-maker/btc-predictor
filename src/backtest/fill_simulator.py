"""Passive limit fill probability and cross-spread instant fills."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from src.backtest.fee_model import FeeModel


class OrderStyle(str, Enum):
  PASSIVE_LIMIT = "passive_limit"
  CROSS_SPREAD = "cross_spread"


@dataclass(frozen=True)
class FillResult:
  filled: bool
  price_cents: int | None
  contracts: int
  is_maker: bool
  fill_probability: float
  skip_reason: str | None = None


@dataclass
class FillSimulatorConfig:
  base_fill_rate: float = 0.55
  spread_distance_penalty: float = 0.08
  time_bonus_per_hour: float = 0.12
  volume_bonus_scale: float = 0.15
  min_fill_probability: float = 0.02
  max_fill_probability: float = 0.98
  default_spread_cents: int = 4
  contracts_per_trade: int = 1
  rng_seed: int | None = None

  @classmethod
  def from_config(cls, cfg: dict[str, Any]) -> FillSimulatorConfig:
    raw = cfg.get("backtest", {}).get("fill_simulator", {})
    return cls(
      base_fill_rate=float(raw.get("base_fill_rate", 0.55)),
      spread_distance_penalty=float(raw.get("spread_distance_penalty", 0.08)),
      time_bonus_per_hour=float(raw.get("time_bonus_per_hour", 0.12)),
      volume_bonus_scale=float(raw.get("volume_bonus_scale", 0.15)),
      min_fill_probability=float(raw.get("min_fill_probability", 0.02)),
      max_fill_probability=float(raw.get("max_fill_probability", 0.98)),
      default_spread_cents=int(raw.get("default_spread_cents", 4)),
      contracts_per_trade=int(raw.get("contracts_per_trade", 1)),
      rng_seed=raw.get("rng_seed"),
    )


class FillSimulator:
  """Model passive limit fills vs cross-spread taker fills."""

  def __init__(
    self,
    cfg: FillSimulatorConfig | None = None,
    fee_model: FeeModel | None = None,
    *,
    app_cfg: dict[str, Any] | None = None,
  ):
    self.cfg = cfg or (FillSimulatorConfig.from_config(app_cfg) if app_cfg else FillSimulatorConfig())
    self.fees = fee_model or (FeeModel(cfg=app_cfg) if app_cfg else FeeModel())
    self._rng = np.random.default_rng(self.cfg.rng_seed)

  def passive_fill_probability(
    self,
    *,
    spread_distance_cents: float,
    time_to_settle_hours: float,
    volume_proxy: float = 1.0,
  ) -> float:
    """Higher when limit is near mid, more time left, and volume is elevated."""
    logit = math.log(self.cfg.base_fill_rate / (1 - self.cfg.base_fill_rate))
    logit -= self.cfg.spread_distance_penalty * max(0.0, spread_distance_cents)
    logit += self.cfg.time_bonus_per_hour * max(0.0, time_to_settle_hours)
    logit += self.cfg.volume_bonus_scale * math.log1p(max(0.0, volume_proxy))
    prob = 1.0 / (1.0 + math.exp(-logit))
    return float(
      np.clip(prob, self.cfg.min_fill_probability, self.cfg.max_fill_probability)
    )

  def _mid_from_prob(self, prob_up: float) -> int:
    return int(np.clip(round(prob_up * 100), 5, 95))

  def _synthetic_book(self, prob_up: float, spread_cents: int | None = None) -> tuple[int, int, int]:
    spread = spread_cents if spread_cents is not None else self.cfg.default_spread_cents
    mid = self._mid_from_prob(prob_up)
    half = max(1, spread // 2)
    bid = max(1, mid - half)
    ask = min(99, mid + (spread - half))
    if ask <= bid:
      ask = min(99, bid + 1)
    return bid, ask, mid

  def simulate_entry(
    self,
    *,
    prob_up: float,
    side: str,
    order_style: OrderStyle,
    time_to_settle_hours: float = 1.0,
    volume_proxy: float = 1.0,
    spread_cents: int | None = None,
    limit_offset_cents: int = 0,
  ) -> FillResult:
    """Simulate entry on YES or NO side."""
    yes_bid, yes_ask, mid = self._synthetic_book(prob_up, spread_cents)
    if side == "yes":
      bid, ask = yes_bid, yes_ask
    else:
      bid, ask = 100 - yes_ask, 100 - yes_bid

    contracts = self.cfg.contracts_per_trade

    if order_style == OrderStyle.CROSS_SPREAD:
      return FillResult(
        filled=True,
        price_cents=ask,
        contracts=contracts,
        is_maker=False,
        fill_probability=1.0,
      )

    limit_price = int(np.clip(bid + limit_offset_cents, 1, ask - 1 if ask > bid else ask))
    spread_distance = abs(mid - limit_price) if side == "yes" else abs((100 - mid) - limit_price)
    fill_prob = self.passive_fill_probability(
      spread_distance_cents=spread_distance,
      time_to_settle_hours=time_to_settle_hours,
      volume_proxy=volume_proxy,
    )
    filled = bool(self._rng.random() < fill_prob)
    return FillResult(
      filled=filled,
      price_cents=limit_price if filled else None,
      contracts=contracts if filled else 0,
      is_maker=True,
      fill_probability=fill_prob,
      skip_reason=None if filled else "passive_no_fill",
    )
