"""15m leg stop: early-slot gate when monitor HOLD and reassess not against."""

from __future__ import annotations

from src.trading.bot_profit_exit import (
  Slot15LegExitConfig,
  evaluate_slot15_leg_stop_loss,
  leg_stop_suppressed_by_early_slot,
)


def _pos(*, side: str = "no", signal: str = "SHORT") -> dict:
  return {"side": side, "signal": signal, "entry_price_cents": 52}


def _monitor(
  *,
  action: str = "HOLD",
  prob_up: float | None = 0.47,
  bet_side: str = "DOWN",
  seconds_remaining: float = 422.0,
) -> dict:
  return {
    "action": action,
    "reassessed_prob_up": prob_up,
    "bet_side": bet_side,
    "seconds_remaining": seconds_remaining,
  }


def test_suppressed_on_early_hold_neutral_reassess():
  """User scenario: 7m left, slot HOLD, 47% UP on DOWN bet — no leg stop."""
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=4)
  assert leg_stop_suppressed_by_early_slot(
    pos=_pos(),
    monitor=_monitor(),
    seconds_remaining=422.0,
    leg_cfg=leg_cfg,
  )
  reason, _ = evaluate_slot15_leg_stop_loss(
    _pos(),
    45,
    leg_cfg,
    gate_early_slot15=True,
    monitor=_monitor(),
    seconds_remaining=422.0,
  )
  assert reason is None


def test_leg_stop_fires_late_in_slot():
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=4)
  reason, detail = evaluate_slot15_leg_stop_loss(
    _pos(),
    45,
    leg_cfg,
    gate_early_slot15=True,
    monitor=_monitor(seconds_remaining=120.0),
    seconds_remaining=120.0,
  )
  assert reason == "LEG STOP"
  assert "leg stop" in detail.lower()


def test_leg_stop_fires_early_when_reassess_against():
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=4)
  assert not leg_stop_suppressed_by_early_slot(
    pos=_pos(),
    monitor=_monitor(prob_up=0.62),
    seconds_remaining=400.0,
    leg_cfg=leg_cfg,
  )
  reason, _ = evaluate_slot15_leg_stop_loss(
    _pos(),
    45,
    leg_cfg,
    gate_early_slot15=True,
    monitor=_monitor(prob_up=0.62),
    seconds_remaining=400.0,
  )
  assert reason == "LEG STOP"


def test_leg_stop_fires_early_when_slot_monitor_cut():
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=4)
  reason, _ = evaluate_slot15_leg_stop_loss(
    _pos(),
    45,
    leg_cfg,
    gate_early_slot15=True,
    monitor=_monitor(action="CUT LOSSES", prob_up=0.47),
    seconds_remaining=400.0,
  )
  assert reason == "LEG STOP"


def test_hourly_trial_not_gated_by_early_slot():
  leg_cfg = Slot15LegExitConfig(leg_stop_loss_cents=4)
  reason, _ = evaluate_slot15_leg_stop_loss(
    _pos(),
    45,
    leg_cfg,
    gate_early_slot15=False,
    monitor=_monitor(),
    seconds_remaining=422.0,
  )
  assert reason == "LEG STOP"
