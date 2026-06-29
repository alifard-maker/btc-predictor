"""Tests for shared bot profit-target and adaptive exit helpers."""

from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  effective_take_profit_pct,
  evaluate_adaptive_profit_exit,
  profit_pct,
  should_take_profit_target,
  should_trail_exit,
  update_position_peaks,
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


def test_trail_exit_on_giveback_from_peak():
  """Peak +$5, current +$2.5 with 40% giveback → exit."""
  settings = _Settings()
  settings.trail_giveback_pct = 0.40
  peaks = update_position_peaks({"peak_unrealized_usd": 0.0, "peak_profit_pct": 0.0}, 5.0, 10.0)
  assert should_trail_exit(
    enabled=True,
    unrealized_usd=2.5,
    cost_usd=10.0,
    peaks=peaks,
    settings=settings,
    min_hold_seconds=0,
    hold_seconds=60.0,
  )


def test_trail_exit_without_reaching_fixed_target():
  """Peaked +12% then faded — trail exits without hitting 25% target."""
  settings = _Settings()
  settings.take_profit_mode = "trailing"
  settings.trail_giveback_pct = 0.35
  peaks = update_position_peaks({"peak_unrealized_usd": 0.0, "peak_profit_pct": 0.0}, 1.2, 10.0)
  assert not should_take_profit_target(
    enabled=True,
    unrealized_usd=0.50,
    cost_usd=10.0,
    take_profit_pct=0.25,
    take_profit_usd=0.0,
    min_hold_seconds=0,
    hold_seconds=60.0,
  )
  assert should_trail_exit(
    enabled=True,
    unrealized_usd=0.50,
    cost_usd=10.0,
    peaks=peaks,
    settings=settings,
    min_hold_seconds=0,
    hold_seconds=60.0,
  )


def test_fixed_target_still_exits_when_hit_before_trail():
  settings = _Settings()
  settings.take_profit_mode = "hybrid"
  peaks = update_position_peaks({"peak_unrealized_usd": 0.0, "peak_profit_pct": 0.0}, 3.0, 10.0)
  reason, detail = evaluate_adaptive_profit_exit(
    settings=settings,
    unrealized_usd=3.0,
    cost_usd=10.0,
    peaks=peaks,
    hold_seconds=60.0,
    ctx=AdaptiveExitContext(),
  )
  assert reason == "PROFIT TARGET"
  assert "+30.0%" in detail


def test_effective_take_profit_pct_tightens_near_period_end():
  settings = _Settings()
  settings.take_profit_mode = "adaptive"
  ctx = AdaptiveExitContext(seconds_remaining=300.0, period_seconds=3600.0)
  effective = effective_take_profit_pct(settings, ctx)
  assert effective < settings.take_profit_pct
  assert effective >= settings.min_take_profit_pct


def test_update_position_peaks_tracks_high_water_mark():
  peaks = update_position_peaks({"peak_unrealized_usd": 0.0, "peak_profit_pct": 0.0}, 4.0, 10.0)
  peaks = update_position_peaks(peaks, 2.0, 10.0)
  assert peaks["peak_unrealized_usd"] == 4.0
  assert peaks["peak_profit_pct"] == 0.4


def test_slot15_leg_take_profit_on_mark_cents():
  from src.trading.bot_profit_exit import evaluate_slot15_leg_take_profit, slot15_leg_exit_config

  cfg = {"intra_slot": {"bot": {"leg_take_profit_cents": 3}}}
  leg_cfg = slot15_leg_exit_config(cfg)
  pos = {"entry_price_cents": 55}
  reason, detail = evaluate_slot15_leg_take_profit(pos, 58, 0.30, leg_cfg)
  assert reason == "LEG TAKE PROFIT"
  assert "+3¢" in detail


def test_slot15_leg_stop_on_drawdown():
  from src.trading.bot_profit_exit import evaluate_slot15_leg_stop_loss, slot15_leg_exit_config

  leg_cfg = slot15_leg_exit_config(None)
  pos = {"entry_price_cents": 55}
  reason, _ = evaluate_slot15_leg_stop_loss(pos, 51, leg_cfg)
  assert reason == "LEG STOP"


def test_slot15_reassess_neutral_take_profit():
  from src.trading.bot_profit_exit import (
    evaluate_slot15_reassess_neutral_take_profit,
    slot15_leg_exit_config,
  )

  leg_cfg = slot15_leg_exit_config({"intra_slot": {"bot": {"reassess_neutral_band": 0.07}}})
  monitor = {"reassessed_prob_up": 0.52, "reassess_summary": "50/50 at close"}
  reason, detail = evaluate_slot15_reassess_neutral_take_profit(
    {"side": "yes"}, 0.40, monitor, leg_cfg,
  )
  assert reason == "REASSESS NEUTRAL TP"
  assert "50/50" in detail
