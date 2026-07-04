"""Tests for P&L-first Phase 0–1 live gates."""

from __future__ import annotations

from src.trading.pnl_first_gates import (
  filter_pnl_first_candidates,
  pnl_first_active,
  pnl_first_entry_block_reason,
  pnl_first_live_ev_block_reason,
  pnl_first_regime_block_reason,
)


def _cfg(profile: str = "pnl_first") -> dict:
  return {
    "hourly": {"bot": {"live_mechanics_profile": profile}},
    "pnl_first": {"live_ev_min_usd_per_contract": 0.02},
    "fees": {"taker_pct": 10.0},
  }


def test_pnl_first_active_only_live_hourly():
  assert pnl_first_active(_cfg(), kind="hourly", mode="live")
  assert not pnl_first_active(_cfg(), kind="hourly", mode="paper")
  assert not pnl_first_active(_cfg("mechanical_fixes"), kind="hourly", mode="live")


def test_blocks_s2_range():
  pick = {"ticker": "KXBTC-26JUL0420-B63150", "strike_type": "between", "model_prob": 0.7}
  reason = pnl_first_entry_block_reason(pick, "yes", _cfg(), kind="hourly", mode="live")
  assert reason == "pnl_first_s2_blocked"


def test_filter_drops_range_candidates():
  thresh = (0.2, {"ticker": "KXBTC-T63299.99", "signal": "BUY YES"}, {})
  rng = (0.15, {"ticker": "KXBTC-B63150", "strike_type": "between", "signal": "BUY YES"}, {})
  out = filter_pnl_first_candidates([thresh, rng], _cfg(), kind="hourly", mode="live")
  assert len(out) == 1
  assert "T" in out[0][1]["ticker"]


def test_live_ev_blocks_marginal():
  pick = {
    "ticker": "KXBTC-T63299.99",
    "model_prob": 0.52,
    "yes_ask": 0.50,
    "no_ask": 0.50,
  }
  reason = pnl_first_live_ev_block_reason(pick, "yes", _cfg())
  assert reason and reason.startswith("pnl_first_live_ev_negative")


def test_taker_only_blocks_passive():
  pick = {"ticker": "KXBTC-T63299.99", "model_prob": 0.72, "yes_ask": 0.55}
  reason = pnl_first_entry_block_reason(
    pick,
    "yes",
    _cfg(),
    kind="hourly",
    mode="live",
    resolved_execution={"execution_mode": "passive_limit", "price_cents": 50},
  )
  assert reason == "pnl_first_taker_only"


def test_regime_enforced_in_free_mode():
  tab = {"live": {"regime": {"allow_trade": False, "reasons": ["Low expected move"]}}}
  reason = pnl_first_regime_block_reason(tab, _cfg(), kind="hourly", mode="live")
  assert reason and reason.startswith("pnl_first_regime_blocked")
