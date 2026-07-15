"""ETH live Strategy 2 (range band) sizing guards."""

from __future__ import annotations

from dataclasses import replace

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.live_range_guards import (
  clamp_range_band_hour_contracts,
  estrat_for_range_scale_in,
  is_range_pick,
  is_threshold_pick,
  open_range_contracts_on_band,
  range_band_hour_cap_block_reason,
  range_band_spot_entry_block_reason,
  range_band_spot_entry_buffer_usd,
  threshold_spot_entry_block_reason,
  threshold_spot_entry_guard_shadow_only,
)
from src.assets import asset_cfg


class _Store:
  def __init__(self, positions=None, resting=None):
    self._positions = positions or []
    self._resting = resting or []

  def open_positions(self, _event):
    return self._positions

  def list_resting_enters(self, _event, *, mode="live"):
    return self._resting


def test_is_range_pick_detects_between_strike():
  assert is_range_pick({"strike_type": "between", "ticker": "KXETH-26JUL0405-B1750"})
  assert not is_range_pick({"strike_type": "greater", "ticker": "KXETHD-26JUL0405-T1760"})


def test_range_band_hour_cap_blocks_when_full():
  cfg = {
    "hourly": {
      "bot": {
        "live_inventory": {"max_contracts_per_range_band_per_hour": 8},
      },
    },
  }
  store = _Store(
    positions=[{
      "market_ticker": "KXETH-26JUL0405-B1750",
      "side": "no",
      "contracts": 8,
    }],
  )
  reason = range_band_hour_cap_block_reason(
    store=store,
    event_ticker="KXETH-26JUL0405",
    market_ticker="KXETH-26JUL0405-B1750",
    side="no",
    open_positions=store.open_positions("KXETH-26JUL0405"),
    cfg=cfg,
    pick={"strike_type": "between", "ticker": "KXETH-26JUL0405-B1750"},
    additional_contracts=1,
  )
  assert reason and reason.startswith("range_band_hour_contract_cap:")


def test_clamp_range_band_hour_contracts_limits_add():
  cfg = {
    "hourly": {
      "bot": {
        "live_inventory": {"max_contracts_per_range_band_per_hour": 8},
      },
    },
  }
  store = _Store(
    positions=[{
      "market_ticker": "KXETH-26JUL0405-B1750",
      "side": "no",
      "contracts": 6,
    }],
  )
  count, fp = clamp_range_band_hour_contracts(
    6,
    6.0,
    store=store,
    event_ticker="KXETH-26JUL0405",
    market_ticker="KXETH-26JUL0405-B1750",
    side="no",
    open_positions=store.open_positions("KXETH-26JUL0405"),
    cfg=cfg,
  )
  assert count == 2
  assert fp == 2.0


def test_open_range_contracts_includes_resting():
  store = _Store(
    positions=[{"market_ticker": "T", "side": "no", "contracts": 2}],
    resting=[{"market_ticker": "T", "side": "no", "contracts": 3}],
  )
  total = open_range_contracts_on_band(
    store, "EV", "T", "no", store.open_positions("EV"),
  )
  assert total == 5.0


def _b63625_pick() -> dict:
  return {
    "strike_type": "between",
    "contract_type": "range",
    "ticker": "KXBTC-26JUL0417-B63625",
    "floor_strike": 63500.0,
    "cap_strike": 63749.99,
  }


def _spot_guard_cfg() -> dict:
  return {
    "hourly": {
      "bot": {
        "live_inventory": {
          "range_band_spot_entry_guard": {
            "enabled": True,
            "min_buffer_usd": 75,
            "sigma_buffer_fraction": 0.20,
          },
        },
      },
    },
  }


def test_range_band_spot_guard_blocks_yes_when_spot_below_floor():
  """Reproduces B63625 — spot ~63200, band floor 63500, model μ edge is misleading."""
  reason = range_band_spot_entry_block_reason(
    pick=_b63625_pick(),
    side="yes",
    spot_price=63200.0,
    terminal_sigma=180.0,
    cfg=_spot_guard_cfg(),
  )
  assert reason and reason.startswith("range_band_spot_below_floor:")


def test_range_band_spot_guard_allows_yes_inside_band():
  assert range_band_spot_entry_block_reason(
    pick=_b63625_pick(),
    side="yes",
    spot_price=63600.0,
    terminal_sigma=180.0,
    cfg=_spot_guard_cfg(),
  ) is None


def test_range_band_spot_guard_allows_yes_near_floor_within_buffer():
  # floor 63500, buffer max(75, 36)=75 → spot 63430 ok
  assert range_band_spot_entry_block_reason(
    pick=_b63625_pick(),
    side="yes",
    spot_price=63430.0,
    terminal_sigma=180.0,
    cfg=_spot_guard_cfg(),
  ) is None


def test_range_band_spot_guard_blocks_no_when_spot_above_cap():
  reason = range_band_spot_entry_block_reason(
    pick=_b63625_pick(),
    side="no",
    spot_price=63900.0,
    terminal_sigma=180.0,
    cfg=_spot_guard_cfg(),
  )
  assert reason and reason.startswith("range_band_spot_above_cap:")


def test_range_band_spot_guard_skips_non_range():
  assert range_band_spot_entry_block_reason(
    pick={"strike_type": "greater", "floor_strike": 63500.0},
    side="yes",
    spot_price=63200.0,
    cfg=_spot_guard_cfg(),
  ) is None


def test_range_band_spot_guard_eth_asset_defaults():
  """ETH ~$3.5k — tighter absolute buffer than BTC."""
  pick = {
    "strike_type": "between",
    "contract_type": "range",
    "ticker": "KXETH-26JUL0417-B3525",
    "floor_strike": 3525.0,
    "cap_strike": 3549.99,
  }
  cfg = asset_cfg(
    {
      "paths": {"logs": "/tmp", "models": "/tmp", "candles": "/tmp"},
      "eth": {"enabled": True},
      "hourly": {"bot": {"live_inventory": {}}},
    },
    "eth",
  )
  # spot $18 below floor; ETH buffer ~$12 (max of $12 floor, 0.18%*3500≈$6.3)
  reason = range_band_spot_entry_block_reason(
    pick=pick,
    side="yes",
    spot_price=3507.0,
    terminal_sigma=45.0,
    cfg=cfg,
    asset="eth",
  )
  assert reason and reason.startswith("range_band_spot_below_floor:")


def test_range_band_spot_guard_spx_by_asset_override():
  cfg = _spot_guard_cfg()
  buf = range_band_spot_entry_buffer_usd(
    spot_price=6000.0,
    terminal_sigma=30.0,
    cfg=cfg,
    asset="spx",
  )
  assert buf >= 25.0


def test_range_band_spot_guard_ndx_defaults_without_config():
  buf = range_band_spot_entry_buffer_usd(
    spot_price=22000.0,
    terminal_sigma=80.0,
    cfg={"_asset": "ndx"},
    asset="ndx",
  )
  assert buf >= 90.0


def test_estrat_for_range_scale_in_disables_scale_in():
  base = EntryStrategyConfig(allow_scale_in=True, scale_in_max_legs_per_ticker=4)
  cfg = {
    "hourly": {
      "bot": {
        "live_inventory": {
          "allow_scale_in_range": False,
          "scale_in_max_legs_per_ticker_range": 1,
        },
      },
    },
  }
  out = estrat_for_range_scale_in(
    base,
    {"strike_type": "between"},
    cfg,
    kind="hourly",
    mode="live",
  )
  assert out.allow_scale_in is False
  assert out.scale_in_max_legs_per_ticker == 1

  unchanged = estrat_for_range_scale_in(
    base,
    {"strike_type": "greater"},
    cfg,
    kind="hourly",
    mode="live",
  )
  assert unchanged.allow_scale_in is True


def _threshold_guard_cfg(*, shadow_only: bool = False) -> dict:
  return {
    "hourly": {
      "bot": {
        "live_inventory": {
          "threshold_spot_entry_guard": {
            "enabled": True,
            "shadow_only": shadow_only,
            "min_buffer_usd": 75,
            "sigma_buffer_fraction": 0.20,
            "min_spot_pct_buffer": 0.12,
          },
        },
      },
    },
  }


def test_is_threshold_pick_not_range():
  assert is_threshold_pick({"strike_type": "greater", "floor_strike": 65000.0})
  assert not is_threshold_pick(
    {"strike_type": "between", "floor_strike": 65000.0, "cap_strike": 65100.0},
  )


def test_threshold_spot_guard_blocks_greater_yes_when_spot_below_floor():
  """Jul 15 leak: BUY YES ≥$65,400 while BRTI ~$65,300 → later LEG STOP spot against."""
  pick = {"strike_type": "greater", "contract_type": "threshold", "floor_strike": 65400.0}
  reason = threshold_spot_entry_block_reason(
    pick=pick,
    side="yes",
    spot_price=65300.0,
    terminal_sigma=80.0,
    cfg=_threshold_guard_cfg(),
  )
  assert reason and reason.startswith("threshold_spot_below_floor:")


def test_threshold_spot_guard_allows_greater_yes_near_floor():
  pick = {"strike_type": "greater", "floor_strike": 65400.0}
  assert threshold_spot_entry_block_reason(
    pick=pick,
    side="yes",
    spot_price=65350.0,
    terminal_sigma=80.0,
    cfg=_threshold_guard_cfg(),
  ) is None


def test_threshold_spot_guard_blocks_greater_no_when_spot_above_floor():
  pick = {"strike_type": "greater", "floor_strike": 65000.0}
  reason = threshold_spot_entry_block_reason(
    pick=pick,
    side="no",
    spot_price=65150.0,
    terminal_sigma=80.0,
    cfg=_threshold_guard_cfg(),
  )
  assert reason and reason.startswith("threshold_spot_above_floor:")


def test_threshold_spot_guard_disabled_by_default():
  pick = {"strike_type": "greater", "floor_strike": 65400.0}
  assert threshold_spot_entry_block_reason(
    pick=pick,
    side="yes",
    spot_price=65000.0,
    cfg={"hourly": {"bot": {"live_inventory": {}}}},
  ) is None


def test_threshold_spot_guard_shadow_only_flag():
  assert threshold_spot_entry_guard_shadow_only(_threshold_guard_cfg(shadow_only=True))
  assert not threshold_spot_entry_guard_shadow_only(_threshold_guard_cfg(shadow_only=False))


def test_threshold_spot_guard_eth_reproduces_1950_otm_yes():
  """ETH Jul 15 09:30 LEG STOP: YES ≥$1,950 while RTI ~$1,934.64 (gap > $12 buffer)."""
  pick = {"strike_type": "greater", "floor_strike": 1950.0}
  cfg = {
    "hourly": {
      "bot": {
        "live_inventory": {
          "threshold_spot_entry_guard": {
            "enabled": True,
            "min_buffer_usd": 12,
            "min_spot_pct_buffer": 0.18,
            "sigma_buffer_fraction": 0.22,
          },
        },
      },
    },
  }
  reason = threshold_spot_entry_block_reason(
    pick=pick,
    side="yes",
    spot_price=1934.64,
    terminal_sigma=8.0,
    cfg=cfg,
    asset="eth",
  )
  assert reason and reason.startswith("threshold_spot_below_floor:")
