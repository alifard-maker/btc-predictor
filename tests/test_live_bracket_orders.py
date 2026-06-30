"""Tests for live resting bracket orders on Kalshi."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.trading.bot_profit_exit import CheapLegExitConfig
from src.trading.live_bracket_orders import (
  LiveRestingExitConfig,
  bracket_take_profit_cents,
  place_live_bracket_orders,
  should_place_resting_exits,
)


def test_bracket_take_profit_cents_clamped():
  assert bracket_take_profit_cents(8, take_profit_pct=0.25, min_take_profit_pct=0.10, max_take_profit_pct=0.40) >= 9


def test_should_place_only_for_cheap_legs():
  cheap = CheapLegExitConfig(max_entry_cents=20, cut_loss_cents=10)
  resting = LiveRestingExitConfig(enabled=True, cheap_leg_only=True)
  assert should_place_resting_exits(entry_cents=15, cheap_cfg=cheap, resting_cfg=resting)
  assert not should_place_resting_exits(entry_cents=45, cheap_cfg=cheap, resting_cfg=resting)


def test_aggressive_exit_limit_cents():
  from src.trading.live_bracket_orders import aggressive_exit_limit_cents

  assert aggressive_exit_limit_cents(32) == 30
  assert aggressive_exit_limit_cents(2) == 1


def test_place_live_bracket_orders_calls_sell_limits():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.create_order.side_effect = [
    {"order": {"order_id": "stop-1"}},
    {"order": {"order_id": "tp-1"}},
  ]
  cheap = CheapLegExitConfig(max_entry_cents=20, cut_loss_cents=10)
  resting = LiveRestingExitConfig(enabled=True, cheap_leg_only=True, bracket_take_profit=True)
  out = place_live_bracket_orders(
    kalshi,
    market_ticker="MKT",
    side="no",
    contracts=5,
    entry_cents=12,
    cheap_cfg=cheap,
    resting_cfg=resting,
    take_profit_pct=0.25,
    min_take_profit_pct=0.10,
    max_take_profit_pct=0.40,
  )
  assert out["stop_order_id"] == "stop-1"
  assert out["take_profit_order_id"] == "tp-1"
  assert kalshi.create_order.call_count == 2
  assert kalshi.create_order.call_args_list[0].kwargs["action"] == "sell"
  assert kalshi.create_order.call_args_list[0].kwargs["no_price"] == 10
