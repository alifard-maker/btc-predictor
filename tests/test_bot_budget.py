"""Unit tests for deploy_bankroll_usd profit-use percentage logic."""

from __future__ import annotations

from src.trading.bot_budget import deploy_bankroll_usd


def test_use_profits_off_loss_reduces_deploy():
  deploy = deploy_bankroll_usd(
    mode="live",
    use_accumulated_profit=False,
    profit_use_pct=100.0,
    max_cap=25.0,
    paper_bankroll_usd=0.0,
    interval_realized_pnl_usd=-5.0,
  )
  assert deploy == 20.0


def test_use_profits_on_100_pct_full_profit_added():
  deploy = deploy_bankroll_usd(
    mode="live",
    use_accumulated_profit=True,
    profit_use_pct=100.0,
    max_cap=25.0,
    paper_bankroll_usd=0.0,
    interval_realized_pnl_usd=5.0,
  )
  assert deploy == 30.0


def test_use_profits_on_50_pct_half_profit_added():
  deploy = deploy_bankroll_usd(
    mode="live",
    use_accumulated_profit=True,
    profit_use_pct=50.0,
    max_cap=25.0,
    paper_bankroll_usd=0.0,
    interval_realized_pnl_usd=10.0,
  )
  assert deploy == 30.0


def test_paper_mode_excess_bankroll_50_pct():
  deploy = deploy_bankroll_usd(
    mode="paper",
    use_accumulated_profit=True,
    profit_use_pct=50.0,
    max_cap=25.0,
    paper_bankroll_usd=35.0,
    interval_realized_pnl_usd=0.0,
  )
  assert deploy == 30.0
