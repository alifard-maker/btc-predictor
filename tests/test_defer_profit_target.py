"""Tests for defer profit-target near settle (paper only)."""

from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  evaluate_adaptive_profit_exit,
  should_defer_profit_target,
)


class _Settings:
  take_profit_enabled = True
  take_profit_mode = "hybrid"
  take_profit_pct = 0.25
  take_profit_usd = 0.0
  min_hold_seconds = 0
  trail_arm_profit_pct = 0.08
  trail_giveback_pct = 0.40
  trail_arm_profit_usd = 0.50
  trail_giveback_usd = 0.0
  min_take_profit_pct = 0.10
  max_take_profit_pct = 0.40


_DEFER_CFG = {
  "pnl_first": {
    "defer_profit_target_minutes_to_settle": 30,
    "defer_profit_target_modes": ["paper"],
  },
}


def test_should_defer_profit_target_paper_with_time_left():
  ctx = AdaptiveExitContext(seconds_remaining=45 * 60.0, period_seconds=3600.0)
  assert should_defer_profit_target(_DEFER_CFG, ctx, "paper") is True


def test_should_defer_profit_target_live_never():
  ctx = AdaptiveExitContext(seconds_remaining=45 * 60.0, period_seconds=3600.0)
  assert should_defer_profit_target(_DEFER_CFG, ctx, "live") is False


def test_should_defer_profit_target_not_when_near_settle():
  ctx = AdaptiveExitContext(seconds_remaining=20 * 60.0, period_seconds=3600.0)
  assert should_defer_profit_target(_DEFER_CFG, ctx, "paper") is False


def test_paper_defers_profit_target_but_trail_still_fires():
  settings = _Settings()
  settings.trail_giveback_pct = 0.35
  ctx = AdaptiveExitContext(seconds_remaining=45 * 60.0, period_seconds=3600.0)

  target_peaks = {"peak_unrealized_usd": 2.6, "peak_profit_pct": 0.26}
  target_reason, _ = evaluate_adaptive_profit_exit(
    settings=settings,
    unrealized_usd=2.6,
    cost_usd=10.0,
    peaks=target_peaks,
    hold_seconds=60.0,
    ctx=ctx,
    cfg=_DEFER_CFG,
    trading_mode="paper",
  )
  assert target_reason is None

  trail_peaks = {"peak_unrealized_usd": 5.0, "peak_profit_pct": 0.5}
  trail_reason, _ = evaluate_adaptive_profit_exit(
    settings=settings,
    unrealized_usd=2.5,
    cost_usd=10.0,
    peaks=trail_peaks,
    hold_seconds=60.0,
    ctx=ctx,
    cfg=_DEFER_CFG,
    trading_mode="paper",
  )
  assert trail_reason == "PROFIT TRAIL"


def test_live_still_hits_profit_target_with_time_left():
  settings = _Settings()
  peaks = {"peak_unrealized_usd": 3.0, "peak_profit_pct": 0.3}
  ctx = AdaptiveExitContext(seconds_remaining=45 * 60.0, period_seconds=3600.0)
  reason, _ = evaluate_adaptive_profit_exit(
    settings=settings,
    unrealized_usd=3.0,
    cost_usd=10.0,
    peaks=peaks,
    hold_seconds=60.0,
    ctx=ctx,
    cfg=_DEFER_CFG,
    trading_mode="live",
  )
  assert reason == "PROFIT TARGET"
