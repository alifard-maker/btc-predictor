"""ETH live Strategy 2 (range band) sizing guards."""

from __future__ import annotations

from dataclasses import replace

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.live_range_guards import (
  clamp_range_band_hour_contracts,
  estrat_for_range_scale_in,
  is_range_pick,
  open_range_contracts_on_band,
  range_band_hour_cap_block_reason,
)


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
