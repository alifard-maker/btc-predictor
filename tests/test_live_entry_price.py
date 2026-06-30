"""Tests for live entry spread-crossing pricing."""

from __future__ import annotations

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.live_entry_price import (
  LiveEntryPricingConfig,
  effective_cross_spread_min_edge_cents,
  format_live_entry_execution_detail,
  resolve_live_entry_price,
)


def _pick(*, model_prob: float, yes_bid: int, yes_ask: int) -> dict:
  return {
    "ticker": "KXTEST-1H-T1",
    "model_prob": model_prob,
    "yes_bid": yes_bid,
    "yes_ask": yes_ask,
    "kalshi_mid": (yes_bid + yes_ask) / 200.0,
  }


def test_cross_spread_when_ask_edge_high_enough():
  pick = _pick(model_prob=0.55, yes_bid=40, yes_ask=42)
  pricing = LiveEntryPricingConfig(cross_spread_enabled=True, cross_spread_min_edge_cents=12.0)
  estrat = EntryStrategyConfig(min_ask_edge_cents=9.0)
  resolved = resolve_live_entry_price(pick, "yes", pricing=pricing, estrat=estrat)
  assert resolved["execution_mode"] == "cross_spread"
  assert resolved["price_cents"] == 42
  assert resolved["ask_edge_cents"] == 13.0


def test_passive_limit_at_mid_when_edge_below_cross_threshold():
  pick = _pick(model_prob=0.50, yes_bid=40, yes_ask=42)
  pricing = LiveEntryPricingConfig(cross_spread_min_edge_cents=12.0)
  estrat = EntryStrategyConfig(min_ask_edge_cents=5.0)
  resolved = resolve_live_entry_price(pick, "yes", pricing=pricing, estrat=estrat)
  assert resolved["execution_mode"] == "passive_limit"
  assert resolved["price_cents"] == 41  # mid of 40/42


def test_cross_threshold_uses_max_of_config_and_entry_gate():
  pricing = LiveEntryPricingConfig(cross_spread_min_edge_cents=10.0)
  estrat = EntryStrategyConfig(min_ask_edge_cents=15.0)
  assert effective_cross_spread_min_edge_cents(pricing, estrat) == 15.0


def test_no_cross_when_disabled():
  pick = _pick(model_prob=0.60, yes_bid=40, yes_ask=42)
  pricing = LiveEntryPricingConfig(cross_spread_enabled=False, cross_spread_min_edge_cents=5.0)
  estrat = EntryStrategyConfig(min_ask_edge_cents=5.0)
  resolved = resolve_live_entry_price(pick, "yes", pricing=pricing, estrat=estrat)
  assert resolved["execution_mode"] == "passive_limit"
  assert resolved["price_cents"] == 41


def test_passive_limit_at_bid():
  pick = _pick(model_prob=0.50, yes_bid=40, yes_ask=42)
  pricing = LiveEntryPricingConfig(
    cross_spread_enabled=False,
    passive_limit_at="bid",
  )
  estrat = EntryStrategyConfig()
  resolved = resolve_live_entry_price(pick, "yes", pricing=pricing, estrat=estrat)
  assert resolved["price_cents"] == 40


def test_format_live_entry_execution_detail_includes_mode():
  detail = format_live_entry_execution_detail({
    "execution_mode": "cross_spread",
    "bid_cents": 40,
    "ask_cents": 42,
    "spread_cents": 2,
    "ask_edge_cents": 14.0,
    "cross_spread_min_edge_cents": 12.0,
  })
  assert "cross_spread" in detail
  assert "ask_edge=14" in detail
