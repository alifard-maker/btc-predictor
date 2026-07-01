"""Tests for rally-only defense skip."""

from __future__ import annotations

from src.trading.live_regime_adaptive import (
  AdaptiveDecision,
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
