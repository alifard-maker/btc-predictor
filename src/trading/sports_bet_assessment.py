"""Phase 1 — annotate sports opportunities with edge tier and quality signals.

Does not block live/paper execution; labels only (mirrors hourly bet_assessment).
"""

from __future__ import annotations

from typing import Any, Sequence

from src.data.sports_markets import SportsEventBook, SportsMarketQuote
from src.trading.sports_value_engine import (
  is_fifa_draw_leg_market,
  is_moneyline_like_market,
)


def assessment_params_from_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  sports = dict((cfg or {}).get("sports") or {})
  raw = dict(sports.get("bet_assessment") or {})
  return {
    "enabled": bool(raw.get("enabled", True)),
    "min_edge_prob_moderate": float(raw.get("min_edge_prob_moderate", 0.03)),
    "min_edge_prob_strong": float(raw.get("min_edge_prob_strong", 0.06)),
    "min_match_score_strong": float(raw.get("min_match_score_strong", 0.70)),
    "min_coherence_moderate": float(raw.get("min_coherence_moderate", 0.50)),
    "min_coherence_strong": float(raw.get("min_coherence_strong", 0.60)),
    "max_spread_cents": float(raw.get("max_spread_cents", 8.0)),
    "min_ask_prob": float(raw.get("min_ask_prob", 0.05)),
    "partition_gap_scale": float(raw.get("partition_gap_scale", 0.15)),
    "max_edge_prob": float(raw.get("max_edge_prob", 0.12)),
  }


def _books_by_event(books: Sequence[SportsEventBook]) -> dict[str, SportsEventBook]:
  out: dict[str, SportsEventBook] = {}
  for b in books:
    key = str(b.event_ticker or "")
    if key:
      out[key] = b
  return out


def _partition_ml_asks(book: SportsEventBook) -> tuple[float | None, int]:
  """Sum yes-asks for plausible exclusive game-ML partition (2- or 3-way)."""
  asks: list[float] = []
  for m in book.markets:
    if is_fifa_draw_leg_market(
      m,
      event_title=book.title or "",
      series_ticker=book.series_ticker or "",
    ):
      if m.yes_ask is not None and 0.01 < float(m.yes_ask) < 0.99:
        asks.append(float(m.yes_ask))
      continue
    if not is_moneyline_like_market(m, event_title=book.title or ""):
      continue
    if m.yes_ask is not None and 0.01 < float(m.yes_ask) < 0.99:
      asks.append(float(m.yes_ask))
  if len(asks) < 2:
    return None, 0
  return sum(asks), len(asks)


def partition_coherence(book: SportsEventBook | None, *, gap_scale: float = 0.15) -> dict[str, Any]:
  """How close Kalshi partition asks sum to $1 (soccer 3-way or 2-way ML)."""
  if book is None:
    return {
      "coherence_score": None,
      "partition_ask_sum": None,
      "partition_outcomes": 0,
      "partition_gap": None,
    }
  ask_sum, n = _partition_ml_asks(book)
  if ask_sum is None:
    return {
      "coherence_score": None,
      "partition_ask_sum": None,
      "partition_outcomes": 0,
      "partition_gap": None,
    }
  gap = abs(float(ask_sum) - 1.0)
  scale = max(0.05, float(gap_scale))
  score = max(0.0, min(1.0, 1.0 - gap / scale))
  return {
    "coherence_score": round(score, 3),
    "partition_ask_sum": round(ask_sum, 4),
    "partition_outcomes": n,
    "partition_gap": round(gap, 4),
  }


def _market_for_opp(
  book: SportsEventBook | None,
  opp: dict[str, Any],
) -> SportsMarketQuote | None:
  if book is None:
    return None
  legs = list(opp.get("legs") or [])
  if not legs:
    return None
  ticker = str((legs[0] or {}).get("ticker") or "")
  if not ticker:
    return None
  for m in book.markets:
    if str(m.ticker) == ticker:
      return m
  return None


def liquidity_signals(
  market: SportsMarketQuote | None,
  opp: dict[str, Any],
  *,
  min_ask_prob: float,
  max_spread_cents: float,
) -> dict[str, Any]:
  ask = float(opp.get("venue_ask") or (market.yes_ask if market else 0) or 0)
  bid = float(market.yes_bid) if market and market.yes_bid is not None else None
  spread_cents = None
  if bid is not None and ask > 0:
    spread_cents = round(max(0.0, (ask - bid) * 100.0), 2)
  penny_ask = ask > 0 and ask < float(min_ask_prob)
  wide_spread = spread_cents is not None and spread_cents > float(max_spread_cents)
  liquidity_ok = not penny_ask and not wide_spread
  return {
    "venue_ask": round(ask, 4) if ask else None,
    "yes_bid": round(bid, 4) if bid is not None else None,
    "spread_cents": spread_cents,
    "penny_ask": penny_ask,
    "wide_spread": wide_spread,
    "liquidity_ok": liquidity_ok,
  }


def _edge_prob_for_opp(opp: dict[str, Any]) -> float | None:
  if opp.get("edge_prob") is not None:
    return float(opp["edge_prob"])
  cost = float(opp.get("total_cost_usd") or 0)
  edge_usd = float(opp.get("edge_usd") or 0)
  if cost > 0 and edge_usd > 0:
    return edge_usd / cost
  edge_pct = opp.get("edge_pct")
  if edge_pct is not None:
    return float(edge_pct)
  return None


def _match_quality_label(match_score: float | None, *, strong_min: float) -> str:
  if match_score is None:
    return "UNKNOWN"
  if match_score >= strong_min:
    return "HIGH"
  if match_score >= strong_min - 0.15:
    return "MODERATE"
  return "LOW"


def assess_sports_opportunity(
  opp: dict[str, Any],
  *,
  book: SportsEventBook | None = None,
  params: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Return bet_assessment block for one opportunity (annotate only)."""
  p = params or assessment_params_from_cfg(None)
  edge_prob = _edge_prob_for_opp(opp)
  match_score = opp.get("match_score")
  match_f = float(match_score) if match_score is not None else None

  coherence = partition_coherence(book, gap_scale=float(p["partition_gap_scale"]))
  coherence_score = coherence.get("coherence_score")

  market = _market_for_opp(book, opp)
  liq = liquidity_signals(
    market,
    opp,
    min_ask_prob=float(p["min_ask_prob"]),
    max_spread_cents=float(p["max_spread_cents"]),
  )

  max_edge = float(p["max_edge_prob"])
  absurd_edge = edge_prob is not None and edge_prob > max_edge

  mod_edge = float(p["min_edge_prob_moderate"])
  strong_edge = float(p["min_edge_prob_strong"])
  strong_match = float(p["min_match_score_strong"])
  mod_coh = float(p["min_coherence_moderate"])
  strong_coh = float(p["min_coherence_strong"])

  coherence_ok_mod = coherence_score is None or float(coherence_score) >= mod_coh
  coherence_ok_strong = coherence_score is None or float(coherence_score) >= strong_coh
  match_ok_strong = match_f is None or match_f >= strong_match

  if edge_prob is None or edge_prob < mod_edge or absurd_edge or not liq["liquidity_ok"]:
    edge_tier = "WEAK"
  elif (
    edge_prob >= strong_edge
    and match_ok_strong
    and coherence_ok_strong
    and liq["liquidity_ok"]
  ):
    edge_tier = "STRONG"
  elif edge_prob >= mod_edge and coherence_ok_mod:
    edge_tier = "MODERATE"
  else:
    edge_tier = "WEAK"

  actionable = edge_tier in ("STRONG", "MODERATE") and not absurd_edge and liq["liquidity_ok"]

  if actionable and edge_tier == "STRONG":
    headline = "STRONG ACTIONABLE BET"
    tone = "strong"
  elif actionable:
    headline = "ACTIONABLE BET"
    tone = "moderate"
  else:
    headline = "NOT STRONG AS AN ACTIONABLE BET"
    tone = "weak"

  detail_parts: list[str] = []
  if edge_prob is not None and edge_prob < mod_edge:
    detail_parts.append(f"Edge {edge_prob * 100:.1f}¢ below {mod_edge * 100:.0f}¢ moderate floor")
  if absurd_edge:
    detail_parts.append(f"Edge {edge_prob * 100:.1f}¢ above absurd cap ({max_edge * 100:.0f}¢)")
  if liq["penny_ask"]:
    detail_parts.append("Penny/dust ask")
  if liq["wide_spread"]:
    detail_parts.append(f"Wide spread ({liq['spread_cents']:.1f}¢)")
  if coherence_score is not None and float(coherence_score) < mod_coh:
    detail_parts.append(f"Partition gap {coherence.get('partition_gap')} (coherence {coherence_score:.2f})")
  if match_f is not None and match_f < strong_match and edge_tier != "STRONG":
    detail_parts.append(f"Match score {match_f:.2f} below strong floor {strong_match:.2f}")

  strat = str(opp.get("strategy") or "")
  return {
    "phase": 1,
    "actionable_bet": actionable,
    "actionable_headline": headline,
    "actionable_tone": tone,
    "edge_tier": edge_tier,
    "edge_tier_label": f"EDGE TIER: {edge_tier}",
    "edge_prob": round(edge_prob, 4) if edge_prob is not None else None,
    "match_score": round(match_f, 3) if match_f is not None else None,
    "match_quality": _match_quality_label(match_f, strong_min=strong_match),
    "coherence_score": coherence_score,
    "partition_ask_sum": coherence.get("partition_ask_sum"),
    "partition_outcomes": coherence.get("partition_outcomes"),
    "partition_gap": coherence.get("partition_gap"),
    "liquidity_ok": liq["liquidity_ok"],
    "spread_cents": liq["spread_cents"],
    "penny_ask": liq["penny_ask"],
    "wide_spread": liq["wide_spread"],
    "absurd_edge": absurd_edge,
    "strategy": strat,
    "detail": " · ".join(detail_parts) if detail_parts else None,
  }


def assess_sports_opportunities(
  opportunities: list[dict[str, Any]],
  *,
  books: Sequence[SportsEventBook] | None = None,
  cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
  """Attach bet_assessment to each opportunity dict (in-place copy)."""
  params = assessment_params_from_cfg(cfg)
  if not params.get("enabled"):
    return opportunities

  by_event = _books_by_event(books or [])
  out: list[dict[str, Any]] = []
  for opp in opportunities:
    enriched = dict(opp)
    event_key = str(opp.get("event_ticker") or "")
    book = by_event.get(event_key)
    enriched["bet_assessment"] = assess_sports_opportunity(
      enriched,
      book=book,
      params=params,
    )
    out.append(enriched)
  return out


def passes_value_strong_bets_gate(opp: dict[str, Any]) -> bool:
  """True when Goal 3 opportunity qualifies as a STRONG assessed bet."""
  if str(opp.get("strategy") or "") != "value_sharp":
    return True
  ba = opp.get("bet_assessment")
  if not isinstance(ba, dict):
    return False
  return bool(ba.get("actionable_bet")) and str(ba.get("edge_tier") or "").upper() == "STRONG"
