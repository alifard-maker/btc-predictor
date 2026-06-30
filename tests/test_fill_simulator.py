"""Tests for passive fill simulator."""

from __future__ import annotations

import numpy as np

from src.backtest.fill_simulator import FillSimulator, FillSimulatorConfig, OrderStyle
from src.backtest.fee_model import FeeModel


def test_cross_spread_always_fills_at_ask():
  sim = FillSimulator(FillSimulatorConfig(rng_seed=1))
  result = sim.simulate_entry(
    prob_up=0.60,
    side="yes",
    order_style=OrderStyle.CROSS_SPREAD,
  )
  assert result.filled is True
  assert result.is_maker is False
  assert result.fill_probability == 1.0
  assert result.price_cents is not None
  assert result.contracts == 1


def test_passive_fill_probability_decreases_with_spread_distance():
  sim = FillSimulator(FillSimulatorConfig())
  near = sim.passive_fill_probability(
    spread_distance_cents=0,
    time_to_settle_hours=1.0,
    volume_proxy=1.0,
  )
  far = sim.passive_fill_probability(
    spread_distance_cents=10,
    time_to_settle_hours=1.0,
    volume_proxy=1.0,
  )
  assert near > far


def test_passive_fill_probability_increases_with_time():
  sim = FillSimulator(FillSimulatorConfig())
  short = sim.passive_fill_probability(
    spread_distance_cents=2,
    time_to_settle_hours=0.25,
    volume_proxy=1.0,
  )
  long = sim.passive_fill_probability(
    spread_distance_cents=2,
    time_to_settle_hours=2.0,
    volume_proxy=1.0,
  )
  assert long > short


def test_passive_fill_reproducible_with_seed():
  cfg = FillSimulatorConfig(rng_seed=99)
  a = FillSimulator(cfg).simulate_entry(
    prob_up=0.55, side="yes", order_style=OrderStyle.PASSIVE_LIMIT
  )
  b = FillSimulator(cfg).simulate_entry(
    prob_up=0.55, side="yes", order_style=OrderStyle.PASSIVE_LIMIT
  )
  assert a.filled == b.filled
  assert a.price_cents == b.price_cents


def test_fee_model_settlement_pnl():
  fees = FeeModel()
  win_pnl = fees.settlement_pnl_usd(
    side="yes",
    entry_price_cents=45,
    contracts=2,
    won=True,
    entry_maker=True,
  )
  loss_pnl = fees.settlement_pnl_usd(
    side="yes",
    entry_price_cents=45,
    contracts=2,
    won=False,
    entry_maker=True,
  )
  assert win_pnl > 0
  assert loss_pnl < 0
  assert win_pnl > loss_pnl
