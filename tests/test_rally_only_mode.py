"""Tests for rally-only defense skip."""

from __future__ import annotations

from src.trading.live_regime_adaptive import (
  AdaptiveDecision,
  assess_adaptive_passive_mode,
  defense_entries_blocked,
)


def test_defense_entries_blocked_when_rally_only_flag():
  cfg = {"hourly": {"bot": {"live_adaptive": {"enabled": True, "defense_skip_all_entries": True}}}}
  defense = AdaptiveDecision("defense", ("regime_blocked",))
  rally = AdaptiveDecision("rally", ("grind_up",))
  assert defense_entries_blocked(defense, cfg) is True
  assert defense_entries_blocked(rally, cfg) is False


def test_defense_entries_not_blocked_without_flag():
  cfg = {"hourly": {"bot": {"live_adaptive": {"enabled": True}}}}
  defense = AdaptiveDecision("defense", ("regime_blocked",))
  assert defense_entries_blocked(defense, cfg) is False


def test_rally_only_paper_mode_classifies_rally_not_adaptive_disabled():
  cfg = {
    "hourly": {
      "bot": {
        "live_adaptive": {
          "enabled": True,
          "defense_skip_all_entries": True,
          "min_rally_grind_pct": 0.10,
        }
      }
    }
  }
  tab = {
    "live": {
      "current_price": 60_200.0,
      "expected_move_pct": 0.20,
      "regime": {"allow_trade": True},
    },
    "locked": {"reference_price": 60_000.0},
  }
  decision = assess_adaptive_passive_mode(
    tab=tab,
    cfg=cfg,
    realized_pnl_usd=0.0,
    aggressive=False,
    mode="paper",
  )
  assert decision.mode == "rally"
  assert defense_entries_blocked(decision, cfg) is False
