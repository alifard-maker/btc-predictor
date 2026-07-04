"""Tests for advanced bot entry strategy (Kelly, ranking, correlation, basket)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.entry_strategy import (
  CycleEntryBudget,
  EntryStrategyConfig,
  ask_edge_cents_for_pick,
  composite_entry_score,
  correlation_block_reason,
  entry_budget_usd,
  is_barbell_pair,
  kelly_stake_usd,
  max_entries_per_cycle_for_family,
  max_entries_per_cycle_for_pick,
  passes_ask_edge_gate,
  passes_tail_entry_gate,
  pick_entry_strategy_family,
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


def test_entry_budget_caps_at_10pct_when_aggressive_preset():
  from src.trading.bot_entry_presets import effective_bot_entry_strategy

  estrat = effective_bot_entry_strategy({}, kind="slot15", aggressive=True, tuning=None)
  pick = _pick("T", model_prob=0.80, ask=0.40)
  stake = entry_budget_usd(
    estrat=estrat,
    bankroll_usd=100.0,
    remaining_usd=100.0,
    pick=pick,
    side="yes",
  )
  assert stake == 10.0


def test_entry_budget_hard_cap_per_order():
  estrat = EntryStrategyConfig(
    enabled=True,
    kelly_enabled=False,
    max_stake_per_entry_usd=10.0,
  )
  pick = _pick("T", model_prob=0.80, ask=0.40)
  stake = entry_budget_usd(
    estrat=estrat,
    bankroll_usd=100.0,
    remaining_usd=50.0,
    pick=pick,
    side="yes",
  )
  assert stake == 10.0


def test_entry_budget_hard_cap_not_below_remaining():
  estrat = EntryStrategyConfig(
    enabled=True,
    kelly_enabled=False,
    max_stake_per_entry_usd=10.0,
  )
  pick = _pick("T", model_prob=0.80, ask=0.40)
  stake = entry_budget_usd(
    estrat=estrat,
    bankroll_usd=5.0,
    remaining_usd=3.0,
    pick=pick,
    side="yes",
  )
  assert stake == 3.0


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
  pick_b = _pick("KX-B", signal="BUY NO", edge=0.11, model_prob=0.30, ask=0.36, floor=59900.0)
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
      aggressive_entries=True,
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
  assert stake == 10.0


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


def test_cap_live_entry_contracts_clamps_for_small_hourly_cap():
  from src.trading.entry_strategy import EntryStrategyConfig, cap_live_entry_contracts

  estrat = EntryStrategyConfig(
    max_stake_per_entry_usd=1.50,
    max_budget_fraction_per_entry=0.30,
    max_contracts_per_entry=4,
  )
  assert cap_live_entry_contracts(
    count=12,
    price_cents=28,
    max_spend_per_hour_usd=5.0,
    estrat=estrat,
  ) == 4


def _range_pick(
  ticker: str,
  *,
  signal: str = "BUY NO",
  floor: float = 58000.0,
  cap: float = 58100.0,
):
  return {
    "ticker": ticker,
    "signal": signal,
    "contract_type": "range",
    "strike_type": "between",
    "floor_strike": floor,
    "cap_strike": cap,
    "edge": 0.10,
    "model_prob": 0.30,
    "yes_bid": 0.35,
    "yes_ask": 0.38,
    "kalshi_mid": 0.38,
  }


def test_correlation_blocks_adjacent_threshold_yes_at_018_gap():
  estrat = EntryStrategyConfig(
    correlation_guard=True,
    correlation_min_strike_gap_pct=0.18,
    allow_barbell=False,
  )
  open_pos = [{"side": "yes", "market_ticker": "LOW"}]
  ex_pick = _pick("LOW", signal="BUY YES", floor=58000.0)
  new_pick = _pick("HIGH", signal="BUY YES", floor=58100.0)

  def resolve(ticker: str):
    return ex_pick if ticker == "LOW" else new_pick

  reason = correlation_block_reason(
    open_pos,
    new_pick,
    "yes",
    resolve_pick=resolve,
    ref_price=58300.0,
    estrat=estrat,
  )
  assert reason == "correlated_same_side_strikes"


def test_correlation_blocks_adjacent_range_no_bands():
  estrat = EntryStrategyConfig(
    correlation_guard=True,
    correlation_min_strike_gap_pct=0.18,
    allow_barbell=False,
  )
  open_pos = [{"side": "no", "market_ticker": "RANGE-A"}]
  ex_pick = _range_pick("RANGE-A", floor=58000.0, cap=58100.0)
  new_pick = _range_pick("RANGE-B", floor=58100.0, cap=58200.0)

  def resolve(ticker: str):
    return ex_pick if ticker == "RANGE-A" else new_pick

  reason = correlation_block_reason(
    open_pos,
    new_pick,
    "no",
    resolve_pick=resolve,
    ref_price=58300.0,
    estrat=estrat,
  )
  assert reason == "correlated_same_side_range_bands"


def test_max_same_side_threshold_legs_blocks_second_yes():
  estrat = EntryStrategyConfig(
    correlation_guard=True,
    correlation_min_strike_gap_pct=0.01,
    max_same_side_threshold_legs=1,
    allow_barbell=False,
  )
  open_pos = [{"side": "yes", "market_ticker": "LOW"}]
  ex_pick = _pick("LOW", signal="BUY YES", floor=57000.0)
  new_pick = _pick("HIGH", signal="BUY YES", floor=59000.0)

  def resolve(ticker: str):
    return ex_pick if ticker == "LOW" else new_pick

  reason = correlation_block_reason(
    open_pos,
    new_pick,
    "yes",
    resolve_pick=resolve,
    ref_price=58300.0,
    estrat=estrat,
  )
  assert reason == "max_same_side_threshold_legs"


def test_pick_entry_strategy_family_classifies_range():
  assert pick_entry_strategy_family(_range_pick("KX-B1")) == "range"
  assert pick_entry_strategy_family(_pick("KX-T1")) == "threshold"
  assert pick_entry_strategy_family({"ticker": "KXBTC-B12"}) == "range"


def test_cycle_entry_budget_independent_family_quotas():
  estrat = EntryStrategyConfig(max_entries_per_cycle=2)
  budget = CycleEntryBudget(estrat)
  th_a = _pick("TH-A", edge=0.20)
  th_b = _pick("TH-B", edge=0.18, floor=59800.0)
  th_c = _pick("TH-C", edge=0.16, floor=59900.0)
  rg_a = _range_pick("RG-A")
  rg_b = _range_pick("RG-B", floor=58200.0, cap=58300.0)

  assert budget.can_enter(th_a)
  budget.record_entry(th_a)
  assert budget.can_enter(th_b)
  budget.record_entry(th_b)
  assert not budget.can_enter(th_c)
  assert budget.can_enter(rg_a)
  budget.record_entry(rg_a)
  assert budget.can_enter(rg_b)
  budget.record_entry(rg_b)
  assert not budget.can_enter(_range_pick("RG-C", floor=58400.0, cap=58500.0))


def test_max_entries_per_cycle_for_pick_uses_family_overrides():
  estrat = EntryStrategyConfig(
    max_entries_per_cycle=2,
    max_threshold_entries_per_cycle=3,
    max_range_entries_per_cycle=1,
  )
  assert max_entries_per_cycle_for_family("threshold", estrat) == 3
  assert max_entries_per_cycle_for_family("range", estrat) == 1
  assert max_entries_per_cycle_for_pick(_pick("T"), estrat) == 3
  assert max_entries_per_cycle_for_pick(_range_pick("R"), estrat) == 1


def test_hourly_s1_full_quota_does_not_block_s2():
  cfg = {
    "hourly": {
      "bot": {
        "entry_strategy": {
          "enabled": True,
          "max_entries_per_cycle": 2,
          "max_concurrent_positions": 6,
          "kelly_enabled": False,
          "correlation_guard": False,
          "min_ask_edge_cents": 0,
        }
      }
    }
  }
  th_a = _pick("KX-TH-A", edge=0.20, floor=59700.0)
  th_b = _pick("KX-TH-B", edge=0.18, floor=59800.0)
  th_c = _pick("KX-TH-C", edge=0.16, floor=59900.0)
  rg_a = _range_pick("KX-RG-A", floor=58000.0, cap=58100.0)
  rg_b = _range_pick("KX-RG-B", floor=58100.0, cap=58200.0)
  tab = {
    "ok": True,
    "event": {"event_ticker": "KXTEST-S1S2"},
    "live": {
      "primary_pick": th_a,
      "current_price": 59800.0,
      "terminal_mu": 59850.0,
      "regime": {"allow_trade": True, "reasons": []},
      "strategy_threshold": {
        "best_edge": th_a,
        "most_likely": th_b,
        "contracts": [th_c],
      },
      "strategy_range": {
        "best_edge": rg_a,
        "most_likely": rg_b,
        "contracts": [],
      },
    },
    "locked": {"reference_price": 59750.0, "primary_pick": th_a},
    "brti_live": 59800.0,
  }
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=50.0,
      allow_strong=False,
      allow_actionable=False,
      aggressive_entries=True,
    ))
    bot = HourlyBot(store, asset="btc")
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    enters = [a for a in actions if a.get("action") == "enter"]
    tickers = {e["market_ticker"] for e in enters}
    assert len(enters) == 4
    assert tickers == {"KX-TH-A", "KX-TH-B", "KX-RG-A", "KX-RG-B"}
    assert "KX-TH-C" not in tickers


def test_hourly_threshold_only_backward_compat():
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
  pick_b = _pick("KX-B", signal="BUY NO", edge=0.11, model_prob=0.30, ask=0.36, floor=59900.0)
  tab = {
    "ok": True,
    "event": {"event_ticker": "KXTEST-TH-ONLY"},
    "live": {
      "primary_pick": pick_a,
      "current_price": 59800.0,
      "terminal_mu": 59850.0,
      "regime": {"allow_trade": True, "reasons": []},
      "strategy_threshold": {
        "best_edge": pick_a,
        "most_likely": pick_a,
        "contracts": [pick_b],
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
      aggressive_entries=True,
    ))
    bot = HourlyBot(store, asset="btc")
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    enters = [a for a in actions if a.get("action") == "enter"]
    assert len(enters) == 2
    tickers = {e["market_ticker"] for e in enters}
    assert tickers == {"KX-A", "KX-B"}
