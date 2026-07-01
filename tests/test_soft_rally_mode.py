"""Tests for soft-rally defense threshold-only entries."""

from __future__ import annotations

from src.backtest.mechanics_profiles import apply_mechanics_profile
from src.trading.live_regime_adaptive import (
  AdaptiveDecision,
  adaptive_defense_entry_block_reason,
  adaptive_passive_config,
)


def test_soft_rally_profile_enables_threshold_only_defense():
  cfg = apply_mechanics_profile({}, "soft_rally")
  acfg = adaptive_passive_config(cfg)
  assert acfg.defense_threshold_only is True
  assert acfg.defense_skip_all_entries is False
  assert acfg.defense_min_ask_edge_cents == 15.0
  assert acfg.defense_yes_mid_min_cents == 40
  assert acfg.defense_yes_mid_max_cents == 80


def test_soft_rally_defense_blocks_range_and_no_legs():
  cfg = apply_mechanics_profile({}, "soft_rally")
  defense = AdaptiveDecision("defense", ("regime_blocked",))
  range_pick = {"strike_type": "between", "signal": "BUY YES", "kalshi_mid": 0.55}
  assert adaptive_defense_entry_block_reason(range_pick, "yes", defense, cfg) == "soft_rally_defense_threshold_only"
  no_pick = {"strike_type": "greater", "signal": "BUY NO", "kalshi_mid": 0.55}
  assert adaptive_defense_entry_block_reason(no_pick, "no", defense, cfg) == "soft_rally_defense_yes_only"


def test_soft_rally_defense_allows_yes_threshold_in_mid_band():
  cfg = apply_mechanics_profile({}, "soft_rally")
  defense = AdaptiveDecision("defense", ("regime_blocked",))
  pick = {"strike_type": "greater", "signal": "BUY YES", "kalshi_mid": 0.55}
  assert adaptive_defense_entry_block_reason(pick, "yes", defense, cfg) is None


def test_soft_rally_defense_blocks_yes_outside_mid_band():
  cfg = apply_mechanics_profile({}, "soft_rally")
  defense = AdaptiveDecision("defense", ("regime_blocked",))
  low = {"strike_type": "greater", "signal": "BUY YES", "kalshi_mid": 0.25}
  assert adaptive_defense_entry_block_reason(low, "yes", defense, cfg) == "soft_rally_defense_mid_band"
