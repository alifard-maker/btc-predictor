"""Tests for :45 ET hourly late-call persistence and bet assessment."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.models.hourly_late_call_log import prediction_to_late_call_row
from src.models.hourly_snapshot import late_call_prediction_from_row
from src.db.hourly_store import SqliteHourlyStore
from src.trading.hourly_bet_assessment import assess_hourly_bet_from_late_call_row


def _base_lock_row() -> dict:
  return {
    "logged_at": "2026-06-28T07:05:00+00:00",
    "event_ticker": "KXBTCD-LATE",
    "frequency": "hourly",
    "settle_time": "2026-06-28T08:00:00+00:00",
    "reference_price": 100000.0,
    "primary_signal": "BUY YES",
    "primary_edge": 0.06,
    "primary_model_prob": 0.62,
    "regime_blocked": 0,
    "expected_move_pct": 0.15,
  }


def _live_pred() -> dict:
  return {
    "current_price": 100200.0,
    "blended_mu": 100350.0,
    "confidence": 0.71,
    "direction": "UP",
    "method": "structure+ml",
    "ml_prob_up": 0.58,
    "prob_15m_avg": 0.55,
    "event": {"event_ticker": "KXBTCD-LATE", "frequency": "hourly", "close_time": "2026-06-28T08:00:00+00:00"},
    "primary_pick": {
      "ticker": "T1",
      "contract_type": "threshold",
      "label": "Above $100k",
      "model_prob": 0.64,
      "kalshi_mid": 0.55,
      "edge": 0.09,
      "signal": "BUY YES",
      "strike_type": "greater",
      "floor_strike": 100000.0,
    },
    "regime": {"allow_trade": True, "reasons": []},
  }


def test_late_call_persistence_immutable_without_force():
  with tempfile.TemporaryDirectory() as tmp:
    db = str(Path(tmp) / "hourly.db")
    store = SqliteHourlyStore(db, asset="btc")
    store.init()
    store.log_prediction(_base_lock_row())

    late_row = prediction_to_late_call_row(_live_pred(), logged_at="2026-06-28T07:45:00+00:00")
    assert store.log_late_call(late_row) is True

    row = store.get_by_event_ticker("KXBTCD-LATE")
    assert row["late_call_logged_at"] == "2026-06-28T07:45:00+00:00"
    assert row["late_call_primary_signal"] == "BUY YES"
    assert row["late_call_primary_edge"] == 0.09
    assert row["primary_signal"] == "BUY YES"  # :05 lock untouched

    late_row2 = prediction_to_late_call_row(
      {**_live_pred(), "primary_pick": {**_live_pred()["primary_pick"], "signal": "BUY NO", "edge": 0.12}},
      logged_at="2026-06-28T07:45:30+00:00",
    )
    assert store.log_late_call(late_row2) is False
    row2 = store.get_by_event_ticker("KXBTCD-LATE")
    assert row2["late_call_primary_signal"] == "BUY YES"


def test_late_call_requires_existing_lock_row():
  with tempfile.TemporaryDirectory() as tmp:
    db = str(Path(tmp) / "hourly.db")
    store = SqliteHourlyStore(db, asset="btc")
    store.init()
    late_row = prediction_to_late_call_row(_live_pred(), logged_at="2026-06-28T07:45:00+00:00")
    assert store.log_late_call(late_row) is False


def test_late_call_snapshot_and_bet_assessment():
  row = {
    **_base_lock_row(),
    "late_call_logged_at": "2026-06-28T07:45:00+00:00",
    "late_call_reference_price": 100200.0,
    "late_call_primary_signal": "BUY NO",
    "late_call_primary_edge": 0.127,
    "late_call_primary_model_prob": 0.38,
    "late_call_primary_label": "Below $100k",
    "late_call_confidence": 0.68,
    "late_call_direction": "DOWN",
    "late_call_method": "structure+ml",
    "late_call_regime_blocked": 1,
    "late_call_regime_notes": "Expected move 0.08% below 0.12% floor; Range compressed",
    "late_call_expected_move_pct": 0.08,
    "late_call_prob_15m_avg": 0.42,
    "late_call_ml_prob_up": 0.41,
  }
  snap = late_call_prediction_from_row(row)
  assert snap is not None
  assert snap["late_call"] is True
  assert snap["primary_pick"]["signal"] == "BUY NO"
  assert snap["bet_assessment"]["actionable_bet"] is False
  assert snap["bet_assessment"]["hour_quality"] == "WEAK"

  assessment = assess_hourly_bet_from_late_call_row(row)
  assert assessment["actionable_headline"] == "NOT STRONG AS AN ACTIONABLE BET"


def test_late_call_strong_actionable_assessment():
  row = {
    "late_call_primary_signal": "BUY YES",
    "late_call_primary_edge": 0.12,
    "late_call_regime_blocked": 0,
    "late_call_regime_notes": "",
    "late_call_expected_move_pct": 0.25,
  }
  result = assess_hourly_bet_from_late_call_row(row)
  assert result["actionable_bet"] is True
  assert result["actionable_headline"] == "STRONG ACTIONABLE BET"
  assert result["hour_quality"] == "STRONG"
