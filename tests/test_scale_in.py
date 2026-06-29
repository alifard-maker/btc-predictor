"""Tests for scale-in (add-to-winner) entry logic."""

from __future__ import annotations

from src.trading.bot_scale_in import evaluate_scale_in
from src.trading.entry_strategy import EntryStrategyConfig, correlation_block_reason


def _pick(
  ticker: str = "KX-T1",
  *,
  signal: str = "BUY YES",
  model_prob: float = 0.70,
  yes_bid: float = 0.55,
  yes_ask: float = 0.58,
):
  return {
    "ticker": ticker,
    "signal": signal,
    "strike_type": "greater",
    "floor_strike": 59700.0,
    "model_prob": model_prob,
    "yes_bid": yes_bid,
    "yes_ask": yes_ask,
    "kalshi_mid": yes_ask,
  }


def _pos(
  *,
  entry_cents: int = 50,
  contracts: int = 10,
  side: str = "yes",
  ticker: str = "KX-T1",
):
  return {
    "market_ticker": ticker,
    "side": side,
    "contracts": contracts,
    "entry_price_cents": entry_cents,
  }


def _estrat(**kwargs) -> EntryStrategyConfig:
  base = {"allow_scale_in": True, "min_ask_edge_cents": 0}
  base.update(kwargs)
  return EntryStrategyConfig(**base)


def test_scale_in_blocked_when_loser():
  pick = _pick(yes_bid=0.48, yes_ask=0.50)
  legs = [_pos(entry_cents=55)]
  ok, reason = evaluate_scale_in(legs, pick, "yes", _estrat())
  assert not ok
  assert reason and reason.startswith("scale_in_not_winner:")


def test_scale_in_allowed_when_winner():
  pick = _pick(yes_bid=0.62, yes_ask=0.65)
  legs = [_pos(entry_cents=50)]
  ok, reason = evaluate_scale_in(legs, pick, "yes", _estrat())
  assert ok
  assert reason is None


def test_scale_in_blocked_when_max_legs_reached():
  pick = _pick(yes_bid=0.62, yes_ask=0.65)
  legs = [_pos(entry_cents=50), _pos(entry_cents=48)]
  ok, reason = evaluate_scale_in(
    legs, pick, "yes", _estrat(scale_in_max_legs_per_ticker=2),
  )
  assert not ok
  assert reason == "scale_in_max_legs"


def test_scale_in_blocked_when_allow_scale_in_false():
  pick = _pick(yes_bid=0.62, yes_ask=0.65)
  legs = [_pos()]
  ok, reason = evaluate_scale_in(legs, pick, "yes", _estrat(allow_scale_in=False))
  assert not ok
  assert reason == "already_open:KX-T1"


def test_correlation_guard_allows_scale_in_same_ticker():
  estrat = EntryStrategyConfig(correlation_guard=True, allow_scale_in=True)
  open_pos = [{"side": "yes", "market_ticker": "KX-T1"}]
  pick = _pick(ticker="KX-T1")

  blocked = correlation_block_reason(
    open_pos,
    pick,
    "yes",
    resolve_pick=lambda t: pick,
    ref_price=59800.0,
    estrat=estrat,
  )
  assert blocked == "duplicate_ticker:KX-T1"

  allowed = correlation_block_reason(
    open_pos,
    pick,
    "yes",
    resolve_pick=lambda t: pick,
    ref_price=59800.0,
    estrat=estrat,
    allow_scale_in_ticker="KX-T1",
  )
  assert allowed is None
