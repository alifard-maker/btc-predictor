"""Tests for adaptive passive rally/defense/lock modes."""

from __future__ import annotations

from dataclasses import replace

from src.trading.bot_entry_presets import effective_bot_entry_strategy
from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.live_entry_price import LiveEntryPricingConfig, live_entry_pricing_from_cfg
from src.trading.live_inventory_guards import apply_live_inventory_guards
from src.trading.live_regime_adaptive import (
  adaptive_live_entry_pricing,
  adaptive_range_band_block_reason,
  apply_adaptive_passive_guards,
  assess_adaptive_passive_mode,
  cross_spread_allowed_for_adaptive,
)


def _tab(
  *,
  regime_allow: bool = True,
  expected_move_pct: float = 0.20,
  current_price: float = 60_200.0,
  ref_price: float = 60_000.0,
  intrahour_highlight: bool = False,
):
  return {
    "live": {
      "current_price": current_price,
      "expected_move_pct": expected_move_pct,
      "terminal_mu": current_price + 50,
      "regime": {"allow_trade": regime_allow, "reasons": []},
      "primary_pick": {
        "ticker": "KXBTCD-T1",
        "signal": "BUY YES",
        "edge": 0.12,
        "strike_type": "greater",
      },
    },
    "locked": {"reference_price": ref_price},
    "intrahour_opportunity": {"highlight": intrahour_highlight},
  }


def _cfg():
  return {
    "hourly": {
      "bot": {
        "live_adaptive": {
          "enabled": True,
          "profit_lock_usd": 1.25,
          "min_rally_expected_move_pct": 0.15,
          "min_rally_grind_pct": 0.10,
        }
      }
    }
  }


def test_assess_rally_on_grind_up():
  decision = assess_adaptive_passive_mode(
    tab=_tab(),
    cfg=_cfg(),
    realized_pnl_usd=0.5,
    aggressive=False,
    mode="live",
  )
  assert decision.mode == "rally"
  assert any("grind_up" in r for r in decision.reasons)


def test_assess_defense_when_regime_blocked():
  decision = assess_adaptive_passive_mode(
    tab=_tab(regime_allow=False, expected_move_pct=0.05, current_price=60_010.0),
    cfg=_cfg(),
    realized_pnl_usd=0.0,
    aggressive=False,
    mode="live",
  )
  assert decision.mode == "defense"
  assert "regime_blocked" in decision.reasons


def test_assess_locked_after_profit_target():
  decision = assess_adaptive_passive_mode(
    tab=_tab(),
    cfg=_cfg(),
    realized_pnl_usd=1.30,
    aggressive=False,
    mode="live",
  )
  assert decision.mode == "locked"


def test_adaptive_disabled_for_aggressive():
  decision = assess_adaptive_passive_mode(
    tab=_tab(),
    cfg=_cfg(),
    realized_pnl_usd=2.0,
    aggressive=True,
    mode="live",
  )
  assert decision.mode == "defense"
  assert decision.reasons == ("adaptive_disabled",)


def test_apply_rally_guards_allow_two_threshold_legs():
  estrat = effective_bot_entry_strategy(_cfg(), kind="hourly", aggressive=False, tuning=None)
  estrat = apply_live_inventory_guards(estrat, _cfg(), mode="live", kind="hourly")
  rally = assess_adaptive_passive_mode(
    tab=_tab(), cfg=_cfg(), realized_pnl_usd=0.0, aggressive=False, mode="live",
  )
  guarded = apply_adaptive_passive_guards(estrat, rally, _cfg())
  assert guarded.max_same_side_threshold_legs == 2
  assert guarded.max_entries_per_cycle == 2
  assert guarded.allow_scale_in is False


def test_apply_defense_guards_tighter():
  estrat = effective_bot_entry_strategy(_cfg(), kind="hourly", aggressive=False, tuning=None)
  estrat = apply_live_inventory_guards(estrat, _cfg(), mode="live", kind="hourly")
  defense = assess_adaptive_passive_mode(
    tab=_tab(regime_allow=False, expected_move_pct=0.05, current_price=60_010.0),
    cfg=_cfg(),
    realized_pnl_usd=0.0,
    aggressive=False,
    mode="live",
  )
  guarded = apply_adaptive_passive_guards(estrat, defense, _cfg())
  assert guarded.max_same_side_threshold_legs == 1
  assert guarded.max_entries_per_cycle == 1
  assert guarded.min_ask_edge_cents >= 12.0


def test_range_band_blocked_in_defense():
  defense = assess_adaptive_passive_mode(
    tab=_tab(regime_allow=False, expected_move_pct=0.05, current_price=60_010.0),
    cfg=_cfg(),
    realized_pnl_usd=0.0,
    aggressive=False,
    mode="live",
  )
  pick = {"strike_type": "between", "ticker": "KXBTC-R1"}
  assert adaptive_range_band_block_reason(pick, defense, _cfg()) == "adaptive_defense_range_blocked"


def test_rally_cross_spread_requires_intrahour_by_default():
  pricing = live_entry_pricing_from_cfg(_cfg(), kind="hourly", aggressive=False)
  rally_no_highlight = assess_adaptive_passive_mode(
    tab=_tab(intrahour_highlight=False),
    cfg=_cfg(),
    realized_pnl_usd=0.0,
    aggressive=False,
    mode="live",
  )
  adapted = adaptive_live_entry_pricing(pricing, rally_no_highlight, _cfg())
  assert adapted.cross_spread_enabled is True
  assert not cross_spread_allowed_for_adaptive(rally_no_highlight, _cfg())

  rally_highlight = replace(rally_no_highlight, intrahour_highlight=True)
  assert cross_spread_allowed_for_adaptive(rally_highlight, _cfg())


def test_threshold_pick_not_blocked():
  defense = assess_adaptive_passive_mode(
    tab=_tab(regime_allow=False, expected_move_pct=0.05, current_price=60_010.0),
    cfg=_cfg(),
    realized_pnl_usd=0.0,
    aggressive=False,
    mode="live",
  )
  pick = {"strike_type": "greater", "ticker": "KXBTC-T1"}
  assert adaptive_range_band_block_reason(pick, defense, _cfg()) is None
