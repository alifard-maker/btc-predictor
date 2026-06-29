"""Tests for advanced bot entry strategy (Kelly, ranking, correlation, basket)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.entry_strategy import (
  EntryStrategyConfig,
  ask_edge_cents_for_pick,
  composite_entry_score,
  correlation_block_reason,
  entry_budget_usd,
  is_barbell_pair,
  kelly_stake_usd,
  passes_ask_edge_gate,
  passes_tail_entry_gate,
  rank_hourly_candidates,
)
from src.trading.hourly_bot import HourlyBot
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def _pick(
  ticker: str,
  *,
  signal: str = "BUY YES",
  edge: float = 0.10,
  model_prob: float = 0.65,
  ask: float = 0.40,
  floor: float = 59700.0,
):
  return {
    "ticker": ticker,
    "signal": signal,
    "strike_type": "greater",
    "floor_strike": floor,
    "edge": edge,
    "model_prob": model_prob,
    "yes_bid": ask - 0.01,
    "yes_ask": ask,
    "kalshi_mid": ask,
    "label": f"≥ ${floor:,.0f}",
  }


def test_rank_prefers_safer_bet_when_edges_tied():
  estrat = EntryStrategyConfig(edge_tie_threshold=0.05, safety_weight=0.5)
  spicy = _pick("SPICY", edge=0.12, model_prob=0.58, ask=0.15, floor=59900)
  safe = _pick("SAFE", edge=0.10, model_prob=0.72, ask=0.55, floor=59700)
  ranked = rank_hourly_candidates(
    [(0.12, spicy, {}), (0.10, safe, {})],
    estrat=estrat,
  )
  assert ranked[0][3]["ticker"] == "SAFE"


def test_kelly_caps_stake_below_full_bankroll():
  stake = kelly_stake_usd(
    bankroll_usd=25.0,
    remaining_usd=25.0,
    p_win=0.62,
    ask_cents=42,
    kelly_fraction=0.25,
    max_budget_fraction_per_entry=0.55,
    min_stake_usd=1.0,
  )
  assert 0 < stake < 25.0


def test_barbell_pair_detected():
  existing_pos = {"side": "yes", "market_ticker": "A"}
  ex_pick = _pick("A", signal="BUY YES", floor=59700.0)
  new_pick = _pick("B", signal="BUY NO", floor=59950.0)
  assert is_barbell_pair(
    existing_pos, ex_pick, new_pick, "no", ref_price=59800.0, min_gap_pct=0.20
  )


def test_correlation_blocks_accidental_hedge():
  estrat = EntryStrategyConfig(allow_barbell=False)
  open_pos = [{"side": "yes", "market_ticker": "LOW"}]
  ex_pick = _pick("LOW", floor=59700.0)
  new_pick = _pick("HIGH", signal="BUY NO", floor=59900.0)

  def resolve(ticker: str):
    return ex_pick if ticker == "LOW" else new_pick

  reason = correlation_block_reason(
    open_pos,
    new_pick,
    "no",
    resolve_pick=resolve,
    ref_price=59800.0,
    estrat=estrat,
  )
  assert reason == "opposing_threshold_hedge"


def test_hourly_multi_entry_two_strikes():
  cfg = {
    "hourly": {
      "bot": {
        "entry_strategy": {
          "enabled": True,
          "max_entries_per_cycle": 2,
          "max_concurrent_positions": 3,
          "kelly_enabled": False,
          "correlation_guard": False,
          "min_ask_edge_cents": 0,
        }
      }
    }
  }
  pick_a = _pick("KX-A", edge=0.14, floor=59700.0)
  pick_b = _pick("KX-B", signal="BUY NO", edge=0.11, model_prob=0.35, ask=0.38, floor=59900.0)
  tab = {
    "ok": True,
    "event": {"event_ticker": "KXTEST-MULTI"},
    "live": {
      "primary_pick": pick_a,
      "current_price": 59800.0,
      "terminal_mu": 59850.0,
      "regime": {"allow_trade": True, "reasons": []},
      "strategy_threshold": {
        "best_edge": pick_a,
        "most_likely": pick_a,
        "contracts": [pick_a, pick_b],
      },
      "strategy_range": {"contracts": []},
    },
    "locked": {"reference_price": 59750.0, "primary_pick": pick_a},
    "brti_live": 59800.0,
  }
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=20.0,
      allow_strong=False,
      allow_actionable=False,
    ))
    bot = HourlyBot(store, asset="btc")
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    enters = [a for a in actions if a.get("action") == "enter"]
    assert len(enters) == 2
    tickers = {e["market_ticker"] for e in enters}
    assert tickers == {"KX-A", "KX-B"}


def test_composite_score_favors_positive_ev():
  estrat = EntryStrategyConfig()
  pick = _pick("X", edge=0.08, model_prob=0.70, ask=0.45)
  score, edge, saf = composite_entry_score(pick, "yes", estrat=estrat)
  assert score > edge
  assert saf > 0.3


def test_entry_budget_falls_back_when_kelly_disabled():
  estrat = EntryStrategyConfig(kelly_enabled=False)
  stake = entry_budget_usd(
    estrat=estrat,
    bankroll_usd=25.0,
    remaining_usd=18.0,
    pick=_pick("Z"),
    side="yes",
  )
  assert stake == 18.0


def test_ask_edge_gate_blocks_thin_edge():
  pick = _pick("T", model_prob=0.55, ask=0.52)
  edge = ask_edge_cents_for_pick(pick, "yes")
  assert edge is not None
  assert edge < 5
  ok, _ = passes_ask_edge_gate(pick, "yes", 8.0)
  assert not ok
  ok2, _ = passes_ask_edge_gate(pick, "yes", 2.0)
  assert ok2


def test_ask_edge_gate_passes_strong_edge():
  pick = _pick("T", model_prob=0.70, ask=0.45)
  edge = ask_edge_cents_for_pick(pick, "yes")
  assert edge is not None
  assert edge >= 20
  ok, _ = passes_ask_edge_gate(pick, "yes", 8.0)
  assert ok


def test_tail_entry_blocked_at_20c():
  estrat = EntryStrategyConfig(
    min_ask_edge_cents=5,
    tail_entry_max_cents=20,
    tail_entry_block=True,
    tail_entry_min_ask_edge_cents=12,
  )
  pick = _pick("T", model_prob=0.70, ask=0.12)
  ok, reason, _ = passes_tail_entry_gate(pick, "yes", 12, estrat)
  assert not ok
  assert reason == "tail_entry_blocked:12c"


def test_tail_entry_allows_mid_bucket_with_base_edge():
  estrat = EntryStrategyConfig(
    min_ask_edge_cents=5,
    tail_entry_max_cents=20,
    tail_entry_block=False,
  )
  pick = _pick("T", model_prob=0.60, ask=0.45)
  ok, reason, _ = passes_tail_entry_gate(pick, "yes", 45, estrat)
  assert ok
  assert reason is None


def test_tail_entry_soft_mode_requires_higher_edge():
  estrat = EntryStrategyConfig(
    min_ask_edge_cents=5,
    tail_entry_max_cents=20,
    tail_entry_block=False,
    tail_entry_min_ask_edge_cents=12,
  )
  pick = _pick("T", model_prob=0.25, ask=0.15)
  ok, reason, _ = passes_tail_entry_gate(pick, "yes", 15, estrat)
  assert not ok
  assert reason and reason.startswith("tail_ask_edge_too_low")

  pick2 = _pick("T", model_prob=0.80, ask=0.15)
  ok2, _, _ = passes_tail_entry_gate(pick2, "yes", 15, estrat)
  assert ok2
