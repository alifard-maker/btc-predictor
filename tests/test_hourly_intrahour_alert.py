"""Tests for live intrahour ETH opportunity highlighting."""

from __future__ import annotations

from src.trading.hourly_intrahour_alert import assess_intrahour_opportunity

CFG = {
  "hourly": {
    "regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12},
    "intrahour": {
      "enabled": True,
      "min_shock_pct": 0.8,
      "min_edge_for_highlight": 0.08,
      "min_edge_override_regime": 0.10,
    },
  }
}


def _locked(ref: float = 2500.0, *, edge: float | None = 0.05) -> dict:
  return {
    "reference_price": ref,
    "logged_at": "2026-06-28T14:05:00+00:00",
    "terminal_mu": ref + 5,
    "regime": {"allow_trade": True, "reasons": []},
    "primary_pick": {"signal": "BUY YES", "edge": edge, "label": "$2,480–$2,500"},
  }


def _live(
  *,
  current: float = 2470.0,
  mu: float = 2505.0,
  signal: str = "BUY YES",
  edge: float = 0.12,
  allow_trade: bool = True,
) -> dict:
  return {
    "current_price": current,
    "terminal_mu": mu,
    "blended_mu": mu,
    "expected_move_pct": (mu - current) / current * 100,
    "primary_pick": {
      "signal": signal,
      "edge": edge,
      "label": "$2,480–$2,500",
      "ticker": "KXETH-TEST",
      "model_prob": 0.62,
    },
    "regime": {"allow_trade": allow_trade, "reasons": []},
    "strategy_threshold": {"best_edge": None},
    "strategy_range": {"best_edge": None},
  }


def test_crash_recovery_strong_edge_highlights():
  result = assess_intrahour_opportunity(
    live=_live(current=2470.0, mu=2505.0, edge=0.12),
    locked=_locked(2500.0),
    current_price=2470.0,
    index_label="ERTI",
    cfg=CFG,
  )
  assert result is not None
  assert result["highlight"] is True
  assert result["trigger"] == "price_shock_recovery"
  assert result["severity"] in ("high", "moderate")
  assert result["actionable_headline"] == "STRONG ACTIONABLE BET"
  assert result["move_pct_since_lock"] < 0
  assert "recovery" in result["detail"].lower()


def test_small_move_no_highlight():
  result = assess_intrahour_opportunity(
    live=_live(current=2495.0, mu=2502.0, edge=0.12),
    locked=_locked(2500.0, edge=0.12),
    current_price=2495.0,
    cfg=CFG,
  )
  assert result is not None
  assert result.get("highlight") is False


def test_big_move_regime_blocked_weak_edge_no_highlight():
  result = assess_intrahour_opportunity(
    live=_live(current=2470.0, mu=2475.0, edge=0.04, allow_trade=False),
    locked=_locked(2500.0),
    current_price=2470.0,
    cfg=CFG,
  )
  assert result is not None
  assert result.get("highlight") is False


def test_edge_spike_without_shock_moderate_highlight():
  result = assess_intrahour_opportunity(
    live=_live(current=2498.0, mu=2510.0, edge=0.11),
    locked=_locked(2500.0, edge=0.05),
    current_price=2498.0,
    cfg=CFG,
  )
  assert result is not None
  assert result.get("highlight") is True
  assert result["trigger"] == "edge_spike"
  assert result["severity"] == "moderate"


def test_disabled_returns_none():
  disabled = {
    "hourly": {
      **CFG["hourly"],
      "intrahour": {**CFG["hourly"]["intrahour"], "enabled": False},
    }
  }
  result = assess_intrahour_opportunity(
    live=_live(),
    locked=_locked(),
    current_price=2470.0,
    cfg=disabled,
  )
  assert result is None


def test_regime_override_large_edge_highlights():
  result = assess_intrahour_opportunity(
    live=_live(current=2470.0, mu=2508.0, edge=0.11, allow_trade=False),
    locked=_locked(2500.0),
    current_price=2470.0,
    cfg=CFG,
  )
  assert result is not None
  assert result["highlight"] is True
  assert result["bet_assessment"].get("regime_overridden") is True
