"""Tests for paper-only split profit-target vs trail min-hold."""

from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  evaluate_adaptive_profit_exit,
  profit_exit_min_hold_seconds,
)


class _Settings:
  take_profit_enabled = True
  take_profit_mode = "hybrid"
  take_profit_pct = 0.25
  take_profit_usd = 0.0
  min_hold_seconds = 90
  trail_arm_profit_pct = 0.08
  trail_giveback_pct = 0.35
  trail_arm_profit_usd = 0.50
  trail_giveback_usd = 0.0
  min_take_profit_pct = 0.10
  max_take_profit_pct = 0.40


_HOLD_CFG = {
  "pnl_first": {
    "paper_profit_exit_hold": {
      "enabled": True,
      "modes": ["paper"],
      "profit_target_min_hold_seconds": 30,
      "trail_min_hold_seconds": 90,
    },
  },
}


def test_profit_exit_min_hold_split_paper():
  s = _Settings()
  assert profit_exit_min_hold_seconds(s, _HOLD_CFG, "paper", for_trail=False) == 30
  assert profit_exit_min_hold_seconds(s, _HOLD_CFG, "paper", for_trail=True) == 90


def test_profit_exit_min_hold_live_unchanged():
  s = _Settings()
  assert profit_exit_min_hold_seconds(s, _HOLD_CFG, "live", for_trail=False) == 90
  assert profit_exit_min_hold_seconds(s, _HOLD_CFG, "live", for_trail=True) == 90


def test_paper_profit_target_fires_at_35s_not_90s():
  """With split hold, paper TP can fire at 35s; live would still need 90s."""
  settings = _Settings()
  peaks = {"peak_unrealized_usd": 1.0, "peak_profit_pct": 0.5}
  ctx = AdaptiveExitContext(seconds_remaining=30 * 60.0, period_seconds=3600.0)
  cost = 2.0
  unrealized = 0.60

  paper_reason, _ = evaluate_adaptive_profit_exit(
    settings=settings,
    unrealized_usd=unrealized,
    cost_usd=cost,
    peaks=peaks,
    hold_seconds=35.0,
    ctx=ctx,
    cfg=_HOLD_CFG,
    trading_mode="paper",
  )
  live_reason, _ = evaluate_adaptive_profit_exit(
    settings=settings,
    unrealized_usd=unrealized,
    cost_usd=cost,
    peaks=peaks,
    hold_seconds=35.0,
    ctx=ctx,
    cfg=_HOLD_CFG,
    trading_mode="live",
  )
  assert paper_reason == "PROFIT TARGET"
  assert live_reason is None
