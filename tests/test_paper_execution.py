from __future__ import annotations

from src.trading.paper_execution import entry_quote_log_fields, format_entry_book_detail, paper_entry_fill, paper_exit_fill


def test_entry_uses_yes_ask_and_sizes_by_budget():
  fill = paper_entry_fill(
    pick={"yes_bid": 0.40, "yes_ask": 0.42},
    side="yes",
    remaining_budget_usd=10.0,
  )
  assert fill["ok"] is True
  assert fill["price_cents"] == 42
  assert fill["bid_cents"] == 40
  assert fill["ask_cents"] == 42
  assert fill["contracts"] == 23


def test_exit_uses_yes_bid():
  fill = paper_exit_fill(pick={"yes_bid": 0.40, "yes_ask": 0.42}, side="yes")
  assert fill["ok"] is True
  assert fill["price_cents"] == 40


def test_no_side_uses_derived_no_bid_ask():
  entry = paper_entry_fill(
    pick={"yes_bid": 0.40, "yes_ask": 0.42},
    side="no",
    remaining_budget_usd=10.0,
  )
  assert entry["ok"] is True
  assert entry["bid_cents"] == 58
  assert entry["ask_cents"] == 60
  assert entry["price_cents"] == 60

  exit_fill = paper_exit_fill(pick={"yes_bid": 0.40, "yes_ask": 0.42}, side="no")
  assert exit_fill["ok"] is True
  assert exit_fill["price_cents"] == 58


def test_entry_rejects_when_spread_too_wide():
  fill = paper_entry_fill(
    pick={"yes_bid": 0.40, "yes_ask": 0.70},
    side="yes",
    remaining_budget_usd=10.0,
  )
  assert fill["ok"] is False
  assert fill["skip_reason"] == "spread_too_wide"


def test_entry_rejects_penny_prices():
  fill = paper_entry_fill(
    pick={"yes_bid": 0.01, "yes_ask": 0.01},
    side="yes",
    remaining_budget_usd=25.0,
  )
  assert fill["ok"] is False
  assert fill["skip_reason"] == "price_floor"


def test_entry_rejects_when_contract_cap_exceeded():
  fill = paper_entry_fill(
    pick={"yes_bid": 0.05, "yes_ask": 0.05},
    side="yes",
    remaining_budget_usd=100.0,
  )
  assert fill["ok"] is False
  assert fill["skip_reason"] == "contract_cap_exceeded"


def test_entry_budget_sizing_uses_ask_price():
  fill = paper_entry_fill(
    pick={"yes_bid": 0.09, "yes_ask": 0.10},
    side="yes",
    remaining_budget_usd=25.0,
  )
  assert fill["ok"] is True
  assert fill["contracts"] == 250
  assert fill["price_cents"] == 10


def test_midpoint_fallback_applies_haircuts():
  entry = paper_entry_fill(
    pick={"kalshi_mid": 0.50},
    side="yes",
    remaining_budget_usd=10.0,
  )
  assert entry["ok"] is True
  assert entry["bid_cents"] == 49
  assert entry["ask_cents"] == 51
  assert entry["price_cents"] == 51

  exit_fill = paper_exit_fill(pick={"kalshi_mid": 0.50}, side="yes")
  assert exit_fill["ok"] is True
  assert exit_fill["price_cents"] == 49


def test_entry_quote_log_fields_and_detail():
  fill = {"bid_cents": 99, "ask_cents": 100}
  fields = entry_quote_log_fields(fill)
  assert fields == {
    "entry_bid_cents": 99,
    "entry_ask_cents": 100,
    "entry_spread_cents": 1,
  }
  assert "bid 99¢" in format_entry_book_detail(fill)
  assert "spread 1¢" in format_entry_book_detail(fill)


def test_rejects_eth_hour_open_style_penny_yes_mispricing():
  """YES on deep ITM strike: real book is bid 99 / ask 100, not 1¢."""
  fill = paper_entry_fill(
    pick={"yes_bid": 0.99, "yes_ask": 1.0},
    side="yes",
    remaining_budget_usd=25.0,
  )
  assert fill["ok"] is True
  assert fill["price_cents"] == 99
  assert fill["contracts"] == 25


def test_entry_accepts_wide_spread_with_custom_max():
  fill = paper_entry_fill(
    pick={"yes_bid": 0.01, "yes_ask": 0.40},
    side="yes",
    remaining_budget_usd=10.0,
    max_spread_cents=40,
  )
  assert fill["ok"] is True
  assert fill["skip_reason"] is None
  assert fill["ask_cents"] - fill["bid_cents"] == 39
