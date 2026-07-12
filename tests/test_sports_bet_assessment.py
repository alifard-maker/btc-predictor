"""Tests for Phase 1 sports bet assessment."""

from src.data.sports_markets import SportsEventBook, SportsMarketQuote
from src.trading.sports_bet_assessment import (
  assess_sports_opportunity,
  assess_sports_opportunities,
  partition_coherence,
)


def _three_way_book(*, tie_ask: float = 0.28) -> SportsEventBook:
  def m(ticker: str, title: str, subtitle: str, ask: float) -> SportsMarketQuote:
    return SportsMarketQuote(
      ticker=ticker,
      event_ticker="KXFIFAGAME-1",
      series_ticker="KXFIFAGAME",
      title=title,
      subtitle=subtitle,
      yes_bid=max(0.01, ask - 0.02),
      yes_ask=ask,
      no_bid=0.5,
      no_ask=0.52,
      close_time=None,
      status="open",
    )

  return SportsEventBook(
    "KXFIFAGAME-1",
    "KXFIFAGAME",
    "Argentina vs Switzerland: Regulation Time Moneyline",
    None,
    [
      m("KXFIFAGAME-1-ARG", "Argentina", "Reg Time: Argentina", 0.46),
      m("KXFIFAGAME-1-SUI", "Switzerland", "Reg Time: Switzerland", 0.28),
      m("KXFIFAGAME-1-TIE", "Tie", "Reg Time: Tie", tie_ask),
    ],
  )


def test_partition_coherence_tight_3way():
  book = _three_way_book(tie_ask=0.26)
  coh = partition_coherence(book)
  assert coh["partition_outcomes"] == 3
  assert coh["partition_ask_sum"] is not None
  assert float(coh["coherence_score"]) >= 0.8


def test_partition_coherence_loose_partition():
  book = _three_way_book(tie_ask=0.10)
  coh = partition_coherence(book)
  assert float(coh["coherence_score"]) < 0.6


def test_strong_value_opportunity_assessment():
  book = _three_way_book()
  opp = {
    "strategy": "value_sharp",
    "kind": "kalshi_value",
    "event_ticker": "KXFIFAGAME-1",
    "series_ticker": "KXFIFAGAME",
    "selection": "Reg Time: Tie",
    "edge_prob": 0.07,
    "edge_usd": 0.35,
    "venue_ask": 0.22,
    "match_score": 0.92,
    "legs": [{"ticker": "KXFIFAGAME-1-TIE", "ask": 0.22}],
  }
  ba = assess_sports_opportunity(opp, book=book)
  assert ba["edge_tier"] == "STRONG"
  assert ba["actionable_bet"] is True
  assert ba["actionable_tone"] == "strong"
  assert ba["match_quality"] == "HIGH"
  assert ba["coherence_score"] is not None


def test_weak_penny_ask_rejected_in_assessment():
  book = _three_way_book()
  opp = {
    "strategy": "value_sharp",
    "event_ticker": "KXFIFAGAME-1",
    "edge_prob": 0.20,
    "venue_ask": 0.02,
    "match_score": 0.9,
    "legs": [{"ticker": "KXFIFAGAME-1-TIE", "ask": 0.02}],
  }
  ba = assess_sports_opportunity(opp, book=book)
  assert ba["edge_tier"] == "WEAK"
  assert ba["actionable_bet"] is False
  assert ba["penny_ask"] is True


def test_dutch_moderate_from_edge_pct():
  opp = {
    "strategy": "dutch_same",
    "kind": "multi_outcome",
    "event_ticker": "KXMLBGAME-1",
    "edge_pct": 0.04,
    "edge_usd": 0.20,
    "total_cost_usd": 5.0,
    "legs": [{"ticker": "KXMLBGAME-1-A", "ask": 0.45}],
  }
  ba = assess_sports_opportunity(opp, book=None)
  assert ba["edge_tier"] == "MODERATE"
  assert ba["actionable_bet"] is True


def test_assess_batch_attaches_bet_assessment():
  book = _three_way_book()
  opps = assess_sports_opportunities(
    [
      {
        "strategy": "value_sharp",
        "event_ticker": "KXFIFAGAME-1",
        "edge_prob": 0.05,
        "venue_ask": 0.24,
        "match_score": 0.8,
        "legs": [{"ticker": "KXFIFAGAME-1-TIE", "ask": 0.24}],
      }
    ],
    books=[book],
    cfg={"sports": {"bet_assessment": {"enabled": True}}},
  )
  assert "bet_assessment" in opps[0]
  assert opps[0]["bet_assessment"]["edge_tier"] in ("MODERATE", "STRONG", "WEAK")


def test_passes_value_strong_bets_gate():
  from src.trading.sports_bet_assessment import passes_value_strong_bets_gate

  strong = {
    "strategy": "value_sharp",
    "bet_assessment": {"actionable_bet": True, "edge_tier": "STRONG"},
  }
  moderate = {
    "strategy": "value_sharp",
    "bet_assessment": {"actionable_bet": True, "edge_tier": "MODERATE"},
  }
  weak = {
    "strategy": "value_sharp",
    "bet_assessment": {"actionable_bet": False, "edge_tier": "WEAK"},
  }
  dutch = {"strategy": "dutch_same"}

  assert passes_value_strong_bets_gate(strong)
  assert not passes_value_strong_bets_gate(moderate)
  assert not passes_value_strong_bets_gate(weak)
  assert not passes_value_strong_bets_gate({"strategy": "value_sharp"})
  assert passes_value_strong_bets_gate(dutch)
