"""Tests for hourly :05 / :45 position alerts (CUT LOSSES / TAKE PROFIT / HOLD)."""

from __future__ import annotations

from src.models.hourly_snapshot import late_call_prediction_from_row, locked_prediction_from_row
from src.trading.hourly_position_alert import (
  assess_hourly_position_alert,
  assess_late_call_position_alert_from_row,
  assess_locked_position_alert_from_row,
)


def test_locked_regime_blocked_buy_no_cut_losses():
  row = {
    "primary_signal": "BUY NO",
    "primary_edge": 0.127,
    "regime_blocked": 1,
    "regime_notes": "Expected move 0.08% below 0.12% floor; Range compressed",
    "expected_move_pct": 0.08,
  }
  result = assess_locked_position_alert_from_row(row)
  assert result["alert"] == "CUT LOSSES"
  assert result["alert_tone"] == "danger"
  assert result["headline"] == "CUT LOSSES"
  assert "Regime blocked" in result["detail"]

  snap = locked_prediction_from_row(row)
  assert snap["position_alert"]["alert"] == "CUT LOSSES"


def test_locked_strong_actionable_hold():
  row = {
    "primary_signal": "BUY YES",
    "primary_edge": 0.12,
    "regime_blocked": 0,
    "regime_notes": "",
    "expected_move_pct": 0.25,
  }
  result = assess_locked_position_alert_from_row(row)
  assert result["alert"] == "HOLD"
  assert result["alert_tone"] == "success"
  assert "Strong actionable lock" in result["detail"]


def test_locked_no_signal_neutral_hold():
  row = {
    "primary_signal": "NEUTRAL",
    "primary_edge": None,
    "regime_blocked": 0,
    "regime_notes": "",
    "expected_move_pct": 0.05,
  }
  result = assess_locked_position_alert_from_row(row)
  assert result["alert"] == "HOLD"
  assert result["alert_tone"] == "neutral"
  assert "no position guidance" in result["detail"].lower()


def test_late_call_regime_blocked_vs_lock_cut_losses():
  row = {
    "primary_signal": "BUY YES",
    "primary_edge": 0.08,
    "reference_price": 100000.0,
    "regime_blocked": 0,
    "terminal_mu": 100100.0,
    "late_call_primary_signal": "BUY YES",
    "late_call_primary_edge": 0.03,
    "late_call_reference_price": 100050.0,
    "late_call_regime_blocked": 1,
    "late_call_regime_notes": "Range compressed",
    "late_call_expected_move_pct": 0.06,
  }
  result = assess_late_call_position_alert_from_row(row)
  assert result["alert"] == "CUT LOSSES"
  assert result["alert_tone"] == "danger"
  assert ":45" in result["detail"] or "Regime blocked" in result["detail"]


def test_late_call_signal_flip_cut_losses():
  row = {
    "primary_signal": "BUY YES",
    "primary_edge": 0.08,
    "reference_price": 100000.0,
    "regime_blocked": 0,
    "late_call_primary_signal": "BUY NO",
    "late_call_primary_edge": 0.09,
    "late_call_reference_price": 99900.0,
    "late_call_regime_blocked": 0,
    "late_call_regime_notes": "",
  }
  result = assess_late_call_position_alert_from_row(row)
  assert result["alert"] == "CUT LOSSES"
  assert "flipped" in result["detail"].lower()


def test_late_call_favorable_take_profit():
  row = {
    "primary_signal": "BUY YES",
    "primary_edge": 0.10,
    "reference_price": 100000.0,
    "regime_blocked": 0,
    "terminal_mu": 100050.0,
    "late_call_logged_at": "2026-06-28T07:45:00+00:00",
    "late_call_primary_signal": "BUY YES",
    "late_call_primary_edge": 0.03,
    "late_call_reference_price": 100080.0,
    "late_call_regime_blocked": 0,
    "late_call_regime_notes": "",
    "late_call_expected_move_pct": 0.08,
  }
  result = assess_late_call_position_alert_from_row(row)
  assert result["alert"] == "TAKE PROFIT"
  assert result["alert_tone"] == "success"
  assert "take profit" in result["detail"].lower() or "captured" in result["detail"].lower()

  snap = late_call_prediction_from_row(row)
  assert snap is not None
  assert snap["position_alert"]["alert"] == "TAKE PROFIT"


def test_late_call_still_aligned_hold():
  row = {
    "primary_signal": "BUY YES",
    "primary_edge": 0.08,
    "reference_price": 100000.0,
    "regime_blocked": 0,
    "terminal_mu": 100040.0,
    "late_call_primary_signal": "BUY YES",
    "late_call_primary_edge": 0.07,
    "late_call_reference_price": 100020.0,
    "late_call_regime_blocked": 0,
    "late_call_regime_notes": "",
    "late_call_expected_move_pct": 0.12,
  }
  result = assess_late_call_position_alert_from_row(row)
  assert result["alert"] == "HOLD"
  assert result["alert_tone"] in ("success", "neutral")


def test_late_call_live_price_enrichment():
  row = {
    "primary_signal": "BUY YES",
    "primary_edge": 0.10,
    "reference_price": 100000.0,
    "regime_blocked": 0,
    "late_call_primary_signal": "BUY YES",
    "late_call_primary_edge": 0.04,
    "late_call_reference_price": 100010.0,
    "late_call_regime_blocked": 0,
    "late_call_regime_notes": "",
  }
  at_late_ref = assess_late_call_position_alert_from_row(row, live_price=100010.0)
  fresher = assess_late_call_position_alert_from_row(row, live_price=100085.0)
  assert fresher["alert"] == "TAKE PROFIT"
  assert at_late_ref["alert"] in ("HOLD", "TAKE PROFIT")
  assert fresher["detail"] != at_late_ref["detail"]


def test_assess_hourly_position_alert_locked_edge_below_min():
  result = assess_hourly_position_alert(
    snapshot_kind="locked",
    signal="BUY YES",
    edge=0.03,
    regime_allow_trade=True,
    regime_reasons=[],
    bet_assessment={
      "actionable_bet": False,
      "hour_quality": "MODERATE",
    },
  )
  assert result["alert"] == "CUT LOSSES"
  assert "below" in result["detail"].lower()


def test_held_band_no_hold_when_spot_above_band():
  from src.trading.hourly_position_alert import assess_held_hourly_position_alert

  pick = {
    "signal": "BUY YES",
    "edge": 0.02,
    "contract_type": "range",
    "strike_type": "between",
    "floor_strike": 1530.0,
    "cap_strike": 1549.99,
  }
  pos = {
    "side": "no",
    "signal": "BUY NO",
    "entry_price_cents": 90,
    "contracts": 2,
  }
  result = assess_held_hourly_position_alert(
    pos=pos,
    pick=pick,
    live_price=1565.0,
    regime_allow_trade=False,
    regime_reasons=["compressed"],
    unrealized_pnl_usd=0.04,
    cfg={"hourly": {"regime": {"min_edge": 0.05}}},
  )
  assert result["alert"] == "HOLD"
  assert "Spot supports" in result["detail"]


def test_unrealized_no_pnl_positive_when_mark_rises():
  from src.trading.paper_execution import unrealized_leg_pnl_usd

  pnl = unrealized_leg_pnl_usd(
    side="no",
    entry_price_cents=90,
    mark_price_cents=92,
    contracts=2,
  )
  assert pnl == 0.04
