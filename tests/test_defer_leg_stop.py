"""Tests for defer LEG STOP / cut-loss when time remains (paper only)."""

from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  Slot15LegExitConfig,
  evaluate_slot15_leg_stop_loss,
  should_defer_leg_stop,
)


_DEFER_CFG = {
  "pnl_first": {
    "defer_leg_stop_minutes_to_settle": 30,
    "defer_leg_stop_modes": ["paper"],
  },
}

_POS = {"entry_price_cents": 60, "side": "yes", "market_ticker": "T"}


def test_should_defer_leg_stop_paper_with_time_left():
  ctx = AdaptiveExitContext(seconds_remaining=45 * 60.0, period_seconds=3600.0)
  assert should_defer_leg_stop(_DEFER_CFG, ctx, "paper") is True


def test_should_defer_leg_stop_live_never():
  ctx = AdaptiveExitContext(seconds_remaining=45 * 60.0, period_seconds=3600.0)
  assert should_defer_leg_stop(_DEFER_CFG, ctx, "live") is False


def test_should_defer_leg_stop_not_when_near_settle():
  ctx = AdaptiveExitContext(seconds_remaining=20 * 60.0, period_seconds=3600.0)
  assert should_defer_leg_stop(_DEFER_CFG, ctx, "paper") is False


def test_paper_defers_leg_stop_with_time_left():
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=8)
  reason, _ = evaluate_slot15_leg_stop_loss(
    _POS,
    50,
    leg_cfg,
    seconds_remaining=45 * 60.0,
    bot_cfg=_DEFER_CFG,
    trading_mode="paper",
  )
  assert reason is None


def test_paper_leg_stop_fires_near_settle():
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=8)
  reason, _ = evaluate_slot15_leg_stop_loss(
    _POS,
    50,
    leg_cfg,
    seconds_remaining=20 * 60.0,
    bot_cfg=_DEFER_CFG,
    trading_mode="paper",
  )
  assert reason == "LEG STOP"
