"""Tests for hourly and 15m actionable bet assessment labels."""

from __future__ import annotations

from types import SimpleNamespace

from src.trading.hourly_bet_assessment import assess_contract_bet, assess_hourly_bet, assess_hourly_bet_from_row
from src.trading.hourly_guidance import build_hourly_guidance, build_range_strategy_guidance
from src.trading.slot15_bet_assessment import assess_slot15_bet, assess_slot15_from_prediction


def test_hourly_regime_blocked_buy_no_is_not_actionable():
  result = assess_hourly_bet(
    signal="BUY NO",
    edge=0.08,
    regime_allow_trade=False,
    regime_reasons=["Expected move 0.05% below 0.12% floor", "Range compressed"],
    expected_move_pct=0.05,
  )
  assert result["actionable_bet"] is False
  assert result["actionable_headline"] == "NOT STRONG AS AN ACTIONABLE BET"
  assert result["hour_quality"] == "WEAK"
  assert result["hour_quality_label"] == "HOUR QUALITY FOR BETTING: WEAK"


def test_hourly_strong_actionable_buy_yes():
  result = assess_hourly_bet(
    signal="BUY YES",
    edge=0.12,
    regime_allow_trade=True,
    regime_reasons=[],
    expected_move_pct=0.25,
  )
  assert result["actionable_bet"] is True
  assert result["actionable_headline"] == "STRONG ACTIONABLE BET"
  assert result["hour_quality"] == "STRONG"


def test_hourly_from_row_regime_blocked():
  row = {
    "primary_signal": "BUY NO",
    "primary_edge": 0.127,
    "regime_blocked": 1,
    "regime_notes": "Expected move 0.08% below 0.12% floor; Range compressed",
    "expected_move_pct": 0.08,
  }
  result = assess_hourly_bet_from_row(row)
  assert result["actionable_bet"] is False
  assert result["hour_quality"] == "WEAK"


def test_slot15_no_trade_when_regime_vetoes():
  result = assess_slot15_bet(
    signal="NO TRADE",
    model_signal="LONG",
    regime_allow_trade=False,
    regime_reasons=["Expected move below fee floor"],
    prob_up=0.62,
    expected_move_pct=0.04,
  )
  assert result["actionable_bet"] is False
  assert result["actionable_headline"] == "NOT STRONG AS AN ACTIONABLE BET"
  assert result["slot_quality"] == "WEAK"
  assert result["slot_quality_label"] == "SLOT QUALITY FOR BETTING: WEAK"
  assert "regime vetoed" in (result["detail"] or "").lower()


def test_slot15_strong_long():
  result = assess_slot15_bet(
    signal="LONG",
    model_signal="LONG",
    regime_allow_trade=True,
    regime_reasons=[],
    prob_up=0.65,
    expected_move_pct=0.15,
  )
  assert result["actionable_bet"] is True
  assert result["actionable_headline"] == "STRONG ACTIONABLE BET"
  assert result["slot_quality"] == "STRONG"


def test_slot15_from_prediction():
  pred = SimpleNamespace(
    price=100_000.0,
    reference_price=100_000.0,
    prob_up=0.65,
    expected_move=150.0,
    signal=SimpleNamespace(value="LONG"),
    model_signal="LONG",
    regime_notes=None,
  )
  cfg = {"min_edge_confidence": 0.57, "intra_slot": {"fee_buffer_pct": 0.08}}
  result = assess_slot15_from_prediction(pred, cfg)
  assert result["actionable_bet"] is True
  assert "SLOT QUALITY FOR BETTING" in result["slot_quality_label"]


def test_assess_contract_bet_uses_locked_regime():
  live = {
    "regime": {"allow_trade": True, "reasons": []},
    "terminal_mu": 100_500,
    "current_price": 100_000,
  }
  locked = {
    "regime": {"allow_trade": False, "reasons": ["Range compressed"]},
    "terminal_mu": 100_200,
    "reference_price": 100_000,
    "expected_move_pct": 0.05,
  }
  result = assess_contract_bet(
    signal="BUY NO",
    edge=0.09,
    live=live,
    locked=locked,
  )
  assert result["actionable_bet"] is False
  assert result["hour_quality"] == "WEAK"


def test_range_strategy_guidance_attaches_bet_assessment():
  live = {
    "strategy_range": {
      "best_edge": {
        "label": "$100,180–$100,200",
        "signal": "BUY NO",
        "edge": 0.11,
        "model_prob": 0.12,
      },
      "most_likely": {
        "label": "$100,180–$100,200",
        "signal": "NEUTRAL",
        "model_prob": 0.42,
      },
    },
    "regime": {"allow_trade": True, "reasons": []},
    "terminal_mu": 100_190,
    "current_price": 100_000,
  }
  out = build_range_strategy_guidance(live, locked=None, index_id="BRTI")
  edge_rec = next(r for r in out["recommendations"] if r["tier"] == "edge")
  assert edge_rec["bet_assessment"]["actionable_headline"] == "STRONG ACTIONABLE BET"
  safest = next(r for r in out["recommendations"] if r["tier"] == "safest")
  assert "bet_assessment" not in safest


def test_hourly_guidance_attaches_bet_assessment_on_edge_tier():
  live = {
    "event": {"frequency": "hourly", "series_ticker": "KXBTCD"},
    "hours_to_settle": 0.8,
    "strategy_threshold": {
      "best_edge": {
        "label": "≥ $100,000",
        "signal": "BUY YES",
        "edge": 0.08,
        "model_prob": 0.58,
        "contract_type": "threshold",
        "floor_strike": 100_000,
      },
    },
    "strategy_range": {},
    "terminal_mu": 100_050,
    "terminal_sigma": 120,
    "regime": {"allow_trade": True, "reasons": []},
    "current_price": 100_000,
  }
  out = build_hourly_guidance(live, locked=None, asset="btc", index_id="BRTI")
  edge_rec = next(r for r in out["recommendations"] if r["tier"] == "edge")
  assert edge_rec["bet_assessment"]["actionable_bet"] is True
  assert "HOUR QUALITY FOR BETTING" in edge_rec["bet_assessment"]["hour_quality_label"]
