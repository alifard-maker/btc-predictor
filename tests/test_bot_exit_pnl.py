"""Tests for effective exit P&L derivation."""

from __future__ import annotations

from src.trading.bot_exit_pnl import effective_exit_pnl_usd


def test_effective_exit_pnl_recomputes_zero_when_prices_differ():
  row = {
    "action": "exit",
    "pnl_usd": 0.0,
    "entry_price_cents": 40,
    "exit_price_cents": 55,
    "contracts": 10,
    "side": "yes",
  }
  assert effective_exit_pnl_usd(row) == 1.5


def test_effective_exit_pnl_keeps_logged_nonzero():
  row = {
    "action": "exit",
    "pnl_usd": -0.12,
    "entry_price_cents": 25,
    "exit_price_cents": 13,
    "contracts": 1,
    "side": "yes",
  }
  assert effective_exit_pnl_usd(row) == -0.12


def test_effective_exit_pnl_true_scratch_stays_zero():
  row = {
    "action": "exit",
    "pnl_usd": 0.0,
    "entry_price_cents": 87,
    "exit_price_cents": 87,
    "contracts": 2,
    "side": "yes",
  }
  assert effective_exit_pnl_usd(row) == 0.0
