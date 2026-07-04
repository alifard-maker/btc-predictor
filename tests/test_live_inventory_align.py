"""live_inventory guard skip rules for Mech vs ETH trial-align."""

from __future__ import annotations

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.live_inventory_guards import apply_live_inventory_guards
from src.trading.hourly_live_trial_align import skip_live_inventory_guards


def test_skip_inventory_only_for_mech_live():
  align_cfg = {
    "hourly": {
      "bot": {
        "live_trial_align": {"enabled": True, "align_live_inventory": True},
        "live_inventory": {"max_same_side_range_legs": 1, "allow_scale_in": False},
      },
    },
  }
  assert not skip_live_inventory_guards(align_cfg, kind="hourly", mode="live")

  mech = dict(align_cfg)
  mech["hourly"]["bot"]["live_mechanics_profile"] = "mechanical_fixes"
  assert skip_live_inventory_guards(mech, kind="hourly", mode="live")


def test_eth_align_applies_inventory_range_cap():
  cfg = {
    "hourly": {
      "bot": {
        "live_trial_align": {"enabled": True, "align_live_inventory": True},
        "live_inventory": {"max_same_side_range_legs": 1, "allow_scale_in": False},
        "entry_strategy": {"max_same_side_range_legs": 0, "allow_scale_in": True},
      },
    },
  }
  base = EntryStrategyConfig(max_same_side_range_legs=0, allow_scale_in=True)
  out = apply_live_inventory_guards(base, cfg, mode="live", kind="hourly")
  assert out.max_same_side_range_legs == 1
  assert out.allow_scale_in is False
