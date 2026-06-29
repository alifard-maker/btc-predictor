"""Tests for shared YES/NO leg P&L (mark and exit use held-side prices)."""

from __future__ import annotations

from src.trading.bot_period_rollover import exit_pnl_usd
from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.paper_execution import leg_pnl_usd, unrealized_leg_pnl_usd
from src.trading.slot15_bot_store import Slot15BotStore


def test_leg_pnl_yes_and_no_use_same_formula():
  assert leg_pnl_usd(entry_price_cents=40, mark_or_exit_cents=55, contracts=10) == 1.5
  assert leg_pnl_usd(entry_price_cents=90, mark_or_exit_cents=92, contracts=2) == 0.04
  assert leg_pnl_usd(entry_price_cents=90, mark_or_exit_cents=85, contracts=2) == -0.10


def test_unrealized_no_matches_leg_pnl():
  assert unrealized_leg_pnl_usd(
    side="no",
    entry_price_cents=90,
    mark_price_cents=92,
    contracts=2,
  ) == 0.04


def test_exit_pnl_usd_no_rollover():
  assert exit_pnl_usd(side="no", contracts=36, entry_cents=68, exit_cents=80) == 4.32


def test_store_exit_pnl_from_prices_no():
  row = {
    "entry_price_cents": 90,
    "exit_price_cents": 92,
    "contracts": 2,
    "side": "no",
  }
  assert HourlyBotStore._exit_pnl_from_prices(row) == 0.04
  assert Slot15BotStore._exit_pnl_from_prices(row) == 0.04
