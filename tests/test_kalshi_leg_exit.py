"""Tests for Kalshi-based leg exit price resolution."""

from __future__ import annotations

from src.trading.kalshi_leg_exit import (
  avg_sell_fill_cents,
  leg_price_cents_from_fill,
  market_binary_exit_cents,
  resolve_kalshi_leg_exit_cents,
)


class _KalshiStub:
  authenticated = True

  def __init__(self, *, fills=None, market=None):
    self._fills = fills or []
    self._market = market

  def list_fills(self, **kwargs):
    return list(self._fills)

  def get(self, path):
    if self._market:
      return {"market": self._market}
    return {}


def test_leg_price_from_no_fill():
  fill = {"yes_price": 70, "side": "no", "action": "sell", "count": 2}
  assert leg_price_cents_from_fill(fill, held_side="no") == 30


def test_avg_sell_fill_cents_weighted():
  kalshi = _KalshiStub(
    fills=[
      {"action": "sell", "side": "no", "yes_price": 70, "count": 1},
      {"action": "sell", "side": "no", "yes_price": 60, "count": 1},
    ],
  )
  assert avg_sell_fill_cents(kalshi, market_ticker="T", side="no") == 35


def test_market_binary_exit_cents():
  kalshi = _KalshiStub(
    market={
      "status": "settled",
      "expiration_value": "59305.24",
      "strike_type": "greater",
      "floor_strike": 59099.99,
    },
  )
  cents, note = market_binary_exit_cents(
    kalshi,
    market_ticker="KXBTCD-26JUN3006-T59099.99",
    side="yes",
    pos={"label": "YES · $59,100 or above"},
  )
  assert cents == 100
  assert "settled" in note.lower()


def test_resolve_prefers_fills_over_settlement():
  kalshi = _KalshiStub(
    fills=[{"action": "sell", "side": "no", "yes_price": 56, "count": 2}],
    market={"status": "settled", "expiration_value": "59305", "strike_type": "greater", "floor_strike": 59400},
  )
  cents, note = resolve_kalshi_leg_exit_cents(
    kalshi,
    market_ticker="T",
    side="no",
    contracts=2,
    pos={"label": "NO · $59,400 or above"},
  )
  assert cents == 44  # NO price = 100 - 56
  assert "fill" in note.lower()
