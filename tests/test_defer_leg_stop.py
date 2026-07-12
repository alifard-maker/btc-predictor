"""Tests for defer LEG STOP / cut-loss when time remains."""

from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  Slot15LegExitConfig,
  evaluate_slot15_leg_stop_loss,
  should_defer_leg_stop,
)


_DEFER_CFG = {
  "pnl_first": {
    "defer_leg_stop_minutes_to_settle": 30,
    "defer_leg_stop_modes": ["paper", "live"],
  },
  "hourly": {"bot": {"live_exit": {"aggressive_exit_haircut_cents": 4}}},
}

_POS = {"entry_price_cents": 60, "side": "yes", "market_ticker": "T"}


def test_should_defer_leg_stop_paper_with_time_left():
  ctx = AdaptiveExitContext(seconds_remaining=45 * 60.0, period_seconds=3600.0)
  assert should_defer_leg_stop(_DEFER_CFG, ctx, "paper") is True


def test_should_defer_leg_stop_live_with_time_left():
  ctx = AdaptiveExitContext(seconds_remaining=45 * 60.0, period_seconds=3600.0)
  assert should_defer_leg_stop(_DEFER_CFG, ctx, "live") is True


def test_should_defer_leg_stop_not_when_near_settle():
  ctx = AdaptiveExitContext(seconds_remaining=20 * 60.0, period_seconds=3600.0)
  assert should_defer_leg_stop(_DEFER_CFG, ctx, "paper") is False
  assert should_defer_leg_stop(_DEFER_CFG, ctx, "live") is False


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


def test_live_defers_leg_stop_with_time_left():
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=4)
  # Mark −12¢ would clear live threshold (4+4 haircut) but defer blocks it
  reason, _ = evaluate_slot15_leg_stop_loss(
    _POS,
    48,
    leg_cfg,
    seconds_remaining=45 * 60.0,
    bot_cfg=_DEFER_CFG,
    trading_mode="live",
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


def test_live_leg_stop_requires_haircut_buffer():
  """Live needs mark drawdown ≥ stop + IOC haircut (default 4+4=8¢)."""
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=4)
  # −4¢ mark: would have fired under old live math; now held
  reason, _ = evaluate_slot15_leg_stop_loss(
    _POS,
    56,
    leg_cfg,
    seconds_remaining=10 * 60.0,
    bot_cfg=_DEFER_CFG,
    trading_mode="live",
  )
  assert reason is None
  # −8¢ mark: fires near settle
  reason, detail = evaluate_slot15_leg_stop_loss(
    _POS,
    52,
    leg_cfg,
    seconds_remaining=10 * 60.0,
    bot_cfg=_DEFER_CFG,
    trading_mode="live",
  )
  assert reason == "LEG STOP"
  assert "haircut" in detail.lower()
