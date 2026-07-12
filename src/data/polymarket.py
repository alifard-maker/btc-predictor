"""Polymarket public market fetch — paper quotes only (no wallet / no orders)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from src.trading.sports_arb_engine import is_prop_like_text

log = logging.getLogger(__name__)

DEFAULT_GAMMA = "https://gamma-api.polymarket.com"

# Odds API sport_key → Polymarket /sports series id (from gamma /sports)
DEFAULT_SPORT_SERIES = {
  "baseball_mlb": "3",
  "soccer_epl": "10188",
  "soccer_fifa_world_cup": "11433",
  "americanfootball_nfl": "10187",
  "tennis_atp": "10365",
  "tennis_wta": "10366",
  "basketball_wnba": "10494",
}


def _poly_series_for_sport_key(sport_key: str, series_map: dict[str, str]) -> str | None:
  if sport_key in series_map:
    return series_map[sport_key]
  if sport_key.startswith("tennis_atp_"):
    return series_map.get("tennis_atp")
  if sport_key.startswith("tennis_wta_"):
    return series_map.get("tennis_wta")
  return None


@dataclass(frozen=True)
class PolyMoneylineQuote:
  event_id: str
  event_slug: str
  title: str
  market_id: str
  question: str
  outcome: str
  ask: float  # probability price to buy this outcome
  bid: float | None
  token_id: str | None
  sport_key: str | None = None

  def to_dict(self) -> dict[str, Any]:
    return {
      "event_id": self.event_id,
      "event_slug": self.event_slug,
      "title": self.title,
      "market_id": self.market_id,
      "question": self.question,
      "outcome": self.outcome,
      "ask": round(self.ask, 4),
      "bid": round(self.bid, 4) if self.bid is not None else None,
      "token_id": self.token_id,
      "sport_key": self.sport_key,
      "venue": "polymarket",
    }


def _parse_json_list(raw: Any) -> list[Any]:
  if raw is None:
    return []
  if isinstance(raw, list):
    return raw
  if isinstance(raw, str):
    try:
      parsed = json.loads(raw)
      return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
      return []
  return []


def _f(raw: Any) -> float | None:
  try:
    if raw is None or raw == "":
      return None
    return float(raw)
  except (TypeError, ValueError):
    return None


class PolymarketClient:
  """Read-only Gamma API client for paper value scanning."""

  def __init__(self, cfg: dict[str, Any] | None = None):
    self.cfg = cfg or {}
    sports = dict(self.cfg.get("sports") or {})
    poly = dict(sports.get("polymarket") or {})
    self.enabled = bool(poly.get("enabled", False))
    self.mode = str(poly.get("mode") or "paper").lower()
    self.base = str(poly.get("gamma_base_url") or DEFAULT_GAMMA).rstrip("/")
    self.cache_sec = float(poly.get("cache_sec", 180))
    series_map = dict(DEFAULT_SPORT_SERIES)
    series_map.update({str(k): str(v) for k, v in (poly.get("sport_series") or {}).items()})
    self.sport_series = series_map
    self._cache: dict[str, tuple[Any, float]] = {}

  @property
  def paper_only(self) -> bool:
    return self.mode != "live"  # live not implemented

  def _cached(self, key: str, fetcher):
    hit = self._cache.get(key)
    if hit and (time.monotonic() - hit[1]) < self.cache_sec:
      return hit[0]
    data = fetcher()
    self._cache[key] = (data, time.monotonic())
    return data

  def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{self.base}{path}"
    resp = requests.get(url, params=params or {}, timeout=25)
    if resp.status_code != 200:
      raise RuntimeError(f"polymarket_{resp.status_code}:{resp.text[:200]}")
    return resp.json()

  def fetch_events_for_series(self, series_id: str, *, limit: int = 40) -> list[dict[str, Any]]:
    def _fetch():
      raw = self._get(
        "/events",
        {
          "series_id": series_id,
          "active": "true",
          "closed": "false",
          "limit": int(limit),
        },
      )
      return raw if isinstance(raw, list) else []

    return self._cached(f"series:{series_id}:{limit}", _fetch)

  def search_events(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    def _fetch():
      raw = self._get("/public-search", {"q": query, "limit_per_type": int(limit)})
      if isinstance(raw, dict):
        return list(raw.get("events") or [])
      return []

    return self._cached(f"search:{query}:{limit}", _fetch)

  def _moneyline_quotes_from_event(
    self,
    event: dict[str, Any],
    *,
    sport_key: str | None = None,
  ) -> list[PolyMoneylineQuote]:
    title = str(event.get("title") or "")
    event_id = str(event.get("id") or "")
    slug = str(event.get("slug") or "")
    markets = event.get("markets") or []
    out: list[PolyMoneylineQuote] = []
    for m in markets:
      if isinstance(m, str):
        try:
          m = json.loads(m)
        except json.JSONDecodeError:
          continue
      if not isinstance(m, dict):
        continue
      question = str(m.get("question") or "")
      # Skip props / spreads / totals / awards / futures — match moneylines only
      q_l = f"{question} {title}".lower()
      if is_prop_like_text(question, title):
        continue
      if any(
        x in q_l
        for x in (
          "spread:",
          "o/u",
          "over/under",
          "1st ",
          "2nd ",
          "3rd ",
          "will there",
          "runs",
          "points",
          "goals",
          "leader",
          "mvp",
          "award",
          "cy young",
          "playoff",
          "futures",
          "division",
          "next team",
          "set 1",
          "set 2",
          "set 3",
          "set winner",
          "method of victory",
          "shootout",
          "penalty",
          "outs",
          "strikeout",
          "stolen",
          "steals",
          "bases",
          "1+",
          "wins by",
          "more than",
          "third place",
          "finisher",
          "outright",
          "championship",
          "winner",
        )
      ):
        continue
      outcomes = [str(x) for x in _parse_json_list(m.get("outcomes"))]
      prices = [_f(x) for x in _parse_json_list(m.get("outcomePrices"))]
      token_ids = [str(x) for x in _parse_json_list(m.get("clobTokenIds"))]
      if len(outcomes) < 2 or len(outcomes) > 3:
        continue
      # Moneyline: outcomes look like team names (not Yes/No), or question ≈ title
      is_yn = {o.lower() for o in outcomes} <= {"yes", "no"}
      if is_yn:
        continue
      best_ask = _f(m.get("bestAsk"))
      best_bid = _f(m.get("bestBid"))
      for i, outcome in enumerate(outcomes):
        ask = prices[i] if i < len(prices) and prices[i] is not None else None
        # Prefer mid/outcome price; if missing use bestAsk only for first outcome (weak)
        if ask is None:
          if best_ask is not None and i == 0:
            ask = best_ask
          else:
            continue
        ask = max(0.01, min(0.99, float(ask)))
        # Dust prices are not tradeable moneyline quotes for value scanning
        if ask < 0.05:
          continue
        bid = None
        if i < len(prices) and prices[i] is not None and best_bid is not None and i == 0:
          bid = best_bid
        elif i < len(prices) and prices[i] is not None:
          # approximate bid slightly inside
          bid = max(0.01, ask - 0.01)
        tok = token_ids[i] if i < len(token_ids) else None
        out.append(
          PolyMoneylineQuote(
            event_id=event_id,
            event_slug=slug,
            title=title,
            market_id=str(m.get("id") or ""),
            question=question,
            outcome=outcome,
            ask=ask,
            bid=bid,
            token_id=tok,
            sport_key=sport_key,
          )
        )
      # Only take first moneyline market per event
      if out:
        break
    return out

  def fetch_moneyline_quotes(
    self,
    sport_keys: list[str],
    *,
    limit_per_sport: int = 40,
  ) -> list[PolyMoneylineQuote]:
    if not self.enabled:
      return []
    quotes: list[PolyMoneylineQuote] = []
    # Dedupe series fetches (many tennis tournament keys → one ATP/WTA series)
    fetched_series: set[str] = set()
    for sk in sport_keys:
      series_id = _poly_series_for_sport_key(sk, self.sport_series)
      events: list[dict[str, Any]] = []
      if series_id and series_id not in fetched_series:
        fetched_series.add(series_id)
        try:
          events = self.fetch_events_for_series(series_id, limit=limit_per_sport)
        except Exception as exc:
          log.warning("polymarket series %s failed: %s", series_id, exc)
      elif series_id:
        # Already fetched this series this call — skip duplicate network
        continue
      if not events and not series_id:
        q = sk.split("_")[-1].upper() if "_" in sk else sk
        try:
          events = self.search_events(q, limit=min(15, limit_per_sport))
        except Exception as exc:
          log.warning("polymarket search %s failed: %s", q, exc)
      # Tag quotes with a stable sport family key for tennis
      tag = sk
      if sk.startswith("tennis_atp_"):
        tag = "tennis_atp"
      elif sk.startswith("tennis_wta_"):
        tag = "tennis_wta"
      for ev in events:
        quotes.extend(self._moneyline_quotes_from_event(ev, sport_key=tag))
    return quotes

  def status(self) -> dict[str, Any]:
    return {
      "enabled": self.enabled,
      "mode": self.mode,
      "paper_only": self.paper_only,
      "gamma_base_url": self.base,
      "sport_series": dict(self.sport_series),
      "live_ready": False,
      "note": "Gamma paper quotes — see clob status for trading auth",
    }
