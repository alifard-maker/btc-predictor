"""Tests for stake cap utilization reporting."""

from __future__ import annotations

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.stake_cap_utilization import compute_stake_cap_utilization


def _enter(cost: float, *, mode: str = "live") -> dict:
  return {
    "action": "enter",
    "status": "filled",
    "mode": mode,
    "cost_usd": cost,
  }


def test_not_binding_when_enters_well_below_cap():
  estrat = EntryStrategyConfig(
    max_stake_per_entry_usd=3.50,
    max_budget_fraction_per_entry=0.10,
  )
  trades = [_enter(0.50), _enter(0.72), _enter(0.60)]
  out = compute_stake_cap_utilization(
    trades, estrat=estrat, max_spend_usd=15.0, mode="live",
  )
  assert out["filled_enters"] == 3
  assert out["cap_binding"] is False
  assert "not binding" in out["summary_line"]


def test_binding_when_many_enters_at_max_stake():
  estrat = EntryStrategyConfig(
    max_stake_per_entry_usd=3.50,
    max_budget_fraction_per_entry=0.25,
  )
  trades = [_enter(3.50), _enter(3.48), _enter(3.50), _enter(1.00)]
  out = compute_stake_cap_utilization(
    trades, estrat=estrat, max_spend_usd=15.0, mode="live",
  )
  assert out["pct_at_max_stake"] >= 0.25
  assert out["cap_binding"] is True
  assert "consider raising" in out["summary_line"]


def test_filters_by_mode():
  estrat = EntryStrategyConfig(max_stake_per_entry_usd=5.0)
  trades = [_enter(4.90, mode="live"), _enter(1.00, mode="paper")]
  out = compute_stake_cap_utilization(
    trades, estrat=estrat, max_spend_usd=15.0, mode="live",
  )
  assert out["filled_enters"] == 1
  assert out["avg_enter_cost_usd"] == 4.90
