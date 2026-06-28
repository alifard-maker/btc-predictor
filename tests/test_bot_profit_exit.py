"""Tests for shared bot profit-target exit helpers."""

from src.trading.bot_profit_exit import profit_pct, should_take_profit_target


def test_should_take_profit_target_pct_only():
  assert should_take_profit_target(
    enabled=True,
    unrealized_usd=3.0,
    cost_usd=10.0,
    take_profit_pct=0.25,
    take_profit_usd=0.0,
    min_hold_seconds=0,
    hold_seconds=60.0,
  )
  assert not should_take_profit_target(
    enabled=True,
    unrealized_usd=2.0,
    cost_usd=10.0,
    take_profit_pct=0.25,
    take_profit_usd=0.0,
    min_hold_seconds=0,
    hold_seconds=60.0,
  )


def test_should_take_profit_target_requires_min_usd_when_set():
  assert not should_take_profit_target(
    enabled=True,
    unrealized_usd=3.0,
    cost_usd=10.0,
    take_profit_pct=0.25,
    take_profit_usd=5.0,
    min_hold_seconds=0,
    hold_seconds=60.0,
  )


def test_profit_pct():
  assert profit_pct(2.5, 10.0) == 0.25
